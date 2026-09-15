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
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Callable

from PyQt6.QtCore import Qt, QEvent, QMargins, QMarginsF, QPoint, QPointF, QRect, QRectF, QSize, QSizeF, QTimer, pyqtSignal
from PyQt6.QtGui import (
    QColor,
    QCursor,
    QGuiApplication,
    QFont,
    QFontMetricsF,
    QIcon,
    QImage,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPen,
    QScreen,
    QTransform,
)
from PyQt6.QtWidgets import (
    QButtonGroup,
    QApplication,
    QColorDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from snipux import design, platform, sensitive, setup_desktop
from snipux.capture import BackendRegistry, CaptureError, Frame
from snipux.chooser import Chooser
from snipux.flowbars import FlowMenu
from snipux.marks import (
    MarkStore,
    TextLabelEditor,
    ToolStyle,
    ToolStyles,
    begin_stroke,
    extend_stroke,
    session_styles,
)
from snipux.shapes import (
    Arrow,
    Blur,
    Crop,
    Ellipse,
    Highlighter,
    Line,
    ObscuringShape,
    Pen,
    Pixelate,
    Rectangle,
    Redact,
    Shape,
    StepMarker,
    Text,
    finalize_mark,
    next_step_number,
    render_selection,
)


class SelectionMode(Enum):
    """How the overlay turns mouse input into a selection.

    Chosen once before the overlay is shown (mirrors how the real app offers
    a mode picker before freezing the screen) — never switched mid-drag.
    """

    RECTANGLE = "rectangle"
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

    def browser_viewport(self) -> "tuple[str, QRectF] | None":
        """`(tab title, absolute logical rect)` of the frontmost browser's
        page area -- everything below the tab strip and toolbars -- or None
        when there is not one to capture.

        Defaulted to None rather than abstract, the same way
        `window_named_at` is defaulted: a provider that only knows window
        geometry keeps working, and "no browser" and "this platform cannot
        tell" are the same answer to the one caller. Wayland is permanently
        the second case, since a client there cannot enumerate other
        applications' windows at all -- which is why the mode greys itself
        out rather than failing.
        """
        return None

    def active_window(self) -> "tuple[str, QRectF] | None":
        """`(title, absolute logical rect)` of the window the user is in, or
        None when there is not one this platform can name.

        The rect is the frame as the compositor draws it: no drop shadow, no
        invisible resize border, and a maximised window's whole frame.

        Never one of snipux's own windows. That is part of the contract, not
        a filter a caller could apply afterwards: once the chooser is up it
        *is* the focused window. So `OverlayWindow` asks once, before it is
        shown, and a provider asked at any other moment answers None rather
        than naming a window of this process.

        Defaulted to None for `browser_viewport`'s reasons. Wayland cannot
        name another application's window at all, and the mode greys itself
        out there rather than failing.
        """
        return None

    def window_named_at(self, point: QPointF) -> "tuple[str, QRectF] | None":
        """The window under `point` as `(title, rect)`, or None.

        The hover preview names what it is about to take -- "snipux --
        notes -- 1433 x 892" in the handoff -- and a rect alone cannot say
        which of two same-sized windows is under the pointer.

        Defaulted rather than abstract so a provider that only knows
        geometry keeps working: it answers with an empty title, and the
        chip degrades to the size it does know.
        """
        rect = self.window_at(point)
        return None if rect is None else ("", rect)


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

    confirmed = pyqtSignal(QRectF)
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

        if self._mode is SelectionMode.WINDOW:
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
            self.confirmed.emit(self._selection)
            return

        if self._mode is SelectionMode.WINDOW and self._window_hit_rect is not None:
            rect = self._window_hit_rect
            self._window_hit_rect = None
            # Unconditional, no distance check, regardless of where the
            # release happened: a window click is a click, not a drag, for
            # its entire duration — the hit was captured at press time and
            # is only read here, never re-queried.
            self.set_selection(rect)
            self.confirmed.emit(rect)
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
        self.confirmed.emit(rect)

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.cancelled.emit()
        elif event.key() in (Qt.Key.Key_Enter, Qt.Key.Key_Return):
            if self._selection is not None:
                self.confirmed.emit(self._selection)
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


# ---------------------------------------------------------------------------
# The stills bar
# ---------------------------------------------------------------------------
# docs/design/bars/README.md, section 2, is the authority, and
# docs/design/bars/divergences.md overrides it wherever the two differ. Every
# size and colour comes from `tokens.BarMetric`/`BarColor`. Two things that
# section calls out because they are easy to get wrong: the bar's fill is a
# 94%-alpha *paint*, never a 94%-*opacity* widget -- that would wash every
# glyph out along with the background; and the bar's position is clamped so
# it can never leave the selection's monitor.

# Reverse of tokens.SHORTCUTS ("P" -> "pen"), so a button can look up its own
# key by tool name instead of every button re-scanning the forward mapping.
_TOOL_SHORTCUT_KEYS = {tool: key for key, tool in design.tokens.SHORTCUTS.items()}

# tokens.SHORTCUTS keyed by Qt.Key code rather than letter (SNX-47), so a
# QKeyEvent.key() is looked up directly instead of going through
# event.text() -- which depends on locale/shift state in a way a letter's key
# code never does.
_SHORTCUT_KEY_CODES = {
    getattr(Qt.Key, f"Key_{letter}"): tool for letter, tool in design.tokens.SHORTCUTS.items()
}
_REDACTION_KEY_CODE = getattr(Qt.Key, f"Key_{design.tokens.REDACTION_KEY}")


def _tool_label(tool: str) -> str:
    """Human-facing text for a `tokens.TOOLS` entry.

    Mirrors editor.py's `_tool_label` (SNX-26) -- same title-casing -- over
    the redesign's plain string tool identifiers rather than the old `Tool`
    enum.
    """
    return tool.replace("_", " ").title()


def _tool_glyph(tool: str) -> str:
    """The glyph `tool` is drawn with: its own name, unless the handoff gave
    it another -- Pixelate is the `mask` glyph, and has no icon of its own.
    """
    return design.tokens.TOOL_GLYPHS.get(tool, tool)


def _family_of(tool: str | None) -> str | None:
    """The family slot `tool` is a sibling in, or None for a tool that has a
    slot to itself.
    """
    for family, siblings in design.tokens.FAMILIES.items():
        if tool in siblings:
            return family
    return None


def _tool_tooltip(tool: str) -> str:
    """The tool's name and the key that reaches it: "Pen — P".

    A redaction sibling names the key its family shares, since that is the
    key that gets there; Crop, which has none, is named alone rather than
    beside a dash leading nowhere.
    """
    name = design.tokens.TOOL_NAMES.get(tool, _tool_label(tool))
    if _family_of(tool) == "redact":
        key = design.tokens.REDACTION_KEY
    else:
        key = _TOOL_SHORTCUT_KEYS.get(tool, "")
    return f"{name} — {key}" if key else name


def _bar_keys() -> list[str]:
    """Every tool key, in the order the bar's slots run -- the shapes
    family's letters where its slot is, and the redaction family's one key
    where that slot is.
    """
    keys = []
    for slot in design.tokens.STILLS_SLOTS:
        if slot == "redact":
            keys.append(design.tokens.REDACTION_KEY)
            continue
        for tool in design.tokens.FAMILIES.get(slot, [slot]):
            if tool in _TOOL_SHORTCUT_KEYS:
                keys.append(_TOOL_SHORTCUT_KEYS[tool])
    return keys


class _Notch(QWidget):
    """The corner of a family slot that opens the family's menu.

    It means one thing -- this slot has more -- so nothing but a family slot
    ever carries one. A child of the slot rather than a hit test inside it:
    a press on the notch is delivered here, so it opens the menu and never
    also arms the tool underneath.
    """

    clicked = pyqtSignal()

    def __init__(self, tooltip: str, parent: QWidget):
        super().__init__(parent)
        metric = design.tokens.BarMetric
        self.setFixedSize(metric.NOTCH, metric.NOTCH)
        corner = metric.BTN - metric.NOTCH_INSET - metric.NOTCH
        self.move(corner, corner)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        # Qt's tooltip wake-up timer is driven by mouse moves over the
        # widget, not by the enter event alone.
        self.setMouseTracking(True)
        self.setToolTip(tooltip)
        self._lit = False

    def set_lit(self, lit: bool) -> None:
        """Follow the slot's own state: a lit slot's notch is lit with it."""
        self._lit = lit
        self.update()

    def mousePressEvent(self, event) -> None:
        event.accept()

    def mouseReleaseEvent(self, event) -> None:
        # Pressing and sliding off is how a user says no.
        if event.button() == Qt.MouseButton.LeftButton and self.rect().contains(
            event.position().toPoint()
        ):
            self.clicked.emit()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        leg = design.tokens.BarMetric.NOTCH_TRIANGLE
        right = float(self.width())
        # Flush right and centred top to bottom in the hit area, which is
        # where the spec's markup lands its triangle: the right angle points
        # into the slot's corner.
        bottom = (self.height() + leg) / 2
        path = QPainterPath()
        path.moveTo(right, bottom - leg)
        path.lineTo(right, bottom)
        path.lineTo(right - leg, bottom)
        path.closeSubpath()
        painter.fillPath(
            path, design.bar_color("NOTCH_ACTIVE" if self._lit else "NOTCH_IDLE")
        )
        painter.end()


class _IconButton(QPushButton):
    rightClicked = pyqtSignal()
    hovered = pyqtSignal(str)
    unhovered = pyqtSignal()

    """One 28px icon button in the stills bar: a tool slot, undo, redo,
    clear or copy. A real `QPushButton`, not a rectangle painted by some
    ancestor's paintEvent, so its tooltip and click handling come for free.

    Idle/hover/active/disabled/danger-hover each recolour the glyph itself,
    not just the background, so a state change regenerates the icon pixmap
    via `design.icon()` rather than leaning on a stylesheet, which has no way
    to reach into an SVG's `currentColor` stroke.
    """

    def __init__(
        self,
        icon_name: str,
        tooltip: str,
        *,
        name: str | None = None,
        idle_color: QColor | None = None,
        hover_bg: QColor | None = None,
        hover_color: QColor | None = None,
        parent=None,
    ):
        super().__init__(parent)
        metric = design.tokens.BarMetric
        self._icon_name = icon_name
        # What hovering reports. For a family slot that is the sibling it
        # would arm, which is not always its glyph's name.
        self._name = name or icon_name
        self._idle_color = idle_color or design.bar_color("TOOL_IDLE_FG")
        self._hover_bg = hover_bg or design.bar_color("TOOL_HOVER_BG")
        # None (every button but Clear) means hovering leaves the glyph's
        # own colour alone -- only Clear's danger hover recolours the icon
        # as well as the background.
        self._hover_color = hover_color
        self._active = False
        self._notch: _Notch | None = None

        self.setFixedSize(metric.BTN, metric.BTN)
        self.setIconSize(QSize(metric.ICON, metric.ICON))
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        # Qt's tooltip wake-up timer is driven by mouse moves over the
        # widget, not by the enter event alone, so a button that never sees
        # one can sit under the cursor without a tooltip ever appearing.
        self.setMouseTracking(True)
        self.setToolTip(tooltip)
        self.setFlat(True)
        self._refresh()

    @property
    def is_active(self) -> bool:
        return self._active

    @property
    def notch(self) -> "_Notch | None":
        return self._notch

    def set_active(self, active: bool) -> None:
        self._active = active
        if self._notch is not None:
            self._notch.set_lit(active)
        self._refresh()

    def setEnabled(self, enabled: bool) -> None:
        super().setEnabled(enabled)
        self._refresh()

    def set_glyph(self, name: str, icon_name: str) -> None:
        """Show `name`'s glyph -- a family slot follows its last-used
        sibling, so it always shows what a drag will draw.
        """
        self._name = name
        self._icon_name = icon_name
        self._refresh()

    def add_notch(self, tooltip: str) -> "_Notch":
        self._notch = _Notch(tooltip, self)
        self._notch.set_lit(self._active)
        return self._notch

    def mousePressEvent(self, event) -> None:
        """A right-click is its own signal.

        A family slot opens its menu from the notch, and from a right-click
        anywhere on it -- the way that menu was reached before the notch
        existed. `QPushButton` reports only left presses, hence this.
        """
        if event.button() == Qt.MouseButton.RightButton:
            self.rightClicked.emit()
            event.accept()
            return
        super().mousePressEvent(event)

    def enterEvent(self, event) -> None:
        self._refresh(hovered=True)
        self.hovered.emit(self._name)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._refresh(hovered=False)
        self.unhovered.emit()
        super().leaveEvent(event)

    def _refresh(self, hovered: bool | None = None) -> None:
        if hovered is None:
            hovered = self.underMouse()
        metric = design.tokens.BarMetric

        if not self.isEnabled():
            # Disabled is the preferred way to show an empty stack, over
            # just recolouring a still-live button -- so this is a real
            # QWidget.setEnabled(False), not a cosmetic-only state.
            bg, glyph = None, design.bar_color("TOOL_DISABLED_FG")
        elif hovered and self._hover_color is not None:
            bg, glyph = self._hover_bg, self._hover_color
        elif self._active:
            bg, glyph = design.bar_color("TOOL_ACTIVE_BG"), design.bar_color("TOOL_ACTIVE_FG")
        elif hovered:
            bg, glyph = self._hover_bg, self._idle_color
        else:
            bg, glyph = None, self._idle_color

        if bg is not None:
            self.setStyleSheet(
                "QPushButton { border: none; border-radius: %dpx;"
                " background: rgba(%d, %d, %d, %s); }"
                % (metric.BTN_RADIUS, bg.red(), bg.green(), bg.blue(), bg.alphaF())
            )
        else:
            self.setStyleSheet("QPushButton { border: none; background: transparent; }")

        # design.icon() only ever fills QIcon.Mode.Normal; left at that,
        # Qt's style would auto-generate its own faded Disabled variant the
        # moment setEnabled(False) runs above, undoing the exact disabled
        # colour just chosen. Registering the same pixmap for both modes
        # makes the disabled state use precisely what was asked for instead
        # of a second, uncontrolled recolouring on top of it.
        pixmap = design.icon(self._icon_name, glyph).pixmap(metric.ICON, metric.ICON)
        icon = QIcon()
        icon.addPixmap(pixmap, QIcon.Mode.Normal)
        icon.addPixmap(pixmap, QIcon.Mode.Disabled)
        self.setIcon(icon)


class _PillButton(QPushButton):
    """The two bar controls that pair an icon with a text label inside a
    solid pill: the capture-mode chip and the review window's Done.

    Built from a child layout of two `QLabel`s rather than
    `QPushButton.setIcon`/`setText`, which always places the icon first --
    the chip needs its chevron *after* the label, per the spec's "label +
    14px chevron".
    """

    def __init__(
        self,
        icon_name: str,
        text: str,
        *,
        icon_size: int,
        text_color: QColor,
        bg_color: QColor,
        icon_after: bool,
        pad_left: int,
        pad_right: int,
        tooltip: str,
        parent=None,
    ):
        super().__init__(parent)
        metric = design.tokens.Metric
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        # Qt's tooltip wake-up timer is driven by mouse moves over the
        # widget, not by the enter event alone, so a button that never sees
        # one can sit under the cursor without a tooltip ever appearing.
        self.setMouseTracking(True)
        self.setToolTip(tooltip)
        self.setFixedHeight(metric.CHIP_H)
        self.setStyleSheet(
            "QPushButton { border: none; border-radius: %dpx;"
            " background: rgba(%d, %d, %d, %s); }"
            % (
                design.tokens.BarMetric.BTN_RADIUS,
                bg_color.red(),
                bg_color.green(),
                bg_color.blue(),
                bg_color.alphaF(),
            )
        )

        layout = QHBoxLayout(self)
        layout.setContentsMargins(pad_left, 0, pad_right, 0)
        layout.setSpacing(6)

        icon_label = QLabel(self)
        icon_label.setPixmap(design.icon(icon_name, text_color).pixmap(icon_size, icon_size))
        # A click must reach the QPushButton underneath, not stop at a
        # child QLabel sitting on top of it.
        icon_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

        self._text_label = QLabel(text, self)
        text_label = self._text_label
        text_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        font = QFont(design.font_families().ui)
        size, weight = design.tokens.Font.CHIP_LABEL
        font.setPixelSize(round(size))
        font.setWeight(QFont.Weight(weight))
        text_label.setFont(font)
        text_label.setStyleSheet(f"color: {text_color.name()};")

        for widget in (text_label, icon_label) if icon_after else (icon_label, text_label):
            layout.addWidget(widget)

    def set_text(self, text: str) -> None:
        """Update the pill's own label -- used by the capture chip (SNX-44)
        to name whichever mode the popover has selected. Done's own
        `_PillButton` never calls this; its label is fixed.
        """
        self._text_label.setText(text)
        self.updateGeometry()

    def sizeHint(self) -> QSize:
        # QPushButton.sizeHint() measures `self.text()`/`self.icon()` --
        # unused here, since the icon+label pairing lives in the child
        # QHBoxLayout instead (see class docstring), so the base
        # implementation falls back to a placeholder "XXXX" string in the
        # button's own default font. That placeholder is what clipped the
        # capture chip and Save down to a few px of label regardless of the
        # word actually on screen (SNX-59): a fixed-looking width that
        # wasn't derived from either the real text or the real font. The
        # child layout already knows the true width, because it was built
        # from `pad_left`/`pad_right`/spacing plus each child's own
        # sizeHint -- and the text label's sizeHint comes from the font
        # it's actually rendering in, fallback or not.
        return self.layout().sizeHint()

    def minimumSizeHint(self) -> QSize:
        return self.sizeHint()


class _Divider(QWidget):
    """1px vertical divider between a bar's or a tray's control groups."""

    def __init__(self, parent=None, *, height: int | None = None, colour: QColor | None = None):
        super().__init__(parent)
        self.setFixedSize(1, design.tokens.Metric.DIVIDER_H if height is None else height)
        colour = colour or design.color("DIVIDER")
        self.setStyleSheet(
            "background: rgba(%d, %d, %d, %s);"
            % (colour.red(), colour.green(), colour.blue(), colour.alphaF())
        )


class _Chrome(QWidget):
    """A chrome widget that sits over the frame and swallows its own presses.

    A Qt widget that leaves a mouse press unaccepted hands it to its
    parent, and every class below is a child of `OverlayWindow` -- whose
    press handler reads a press as ink, a resize, or the start of a fresh
    region drag, depending on where it lands, and closes any menu that is
    open. A click that missed a tool button by a pixel and hit the bar's own
    background therefore threw away the selection the user had just dragged
    out and started a new one under the bar; a press reaching it from a menu
    row would tear the menu down before the row's click landed, so every row
    would look dead. Chrome is opaque: a press that lands on it stops there.

    Only the containers need this. The dividers, pills and separators they
    hold are children of a container that consumes, so their presses stop
    at the same place. The cost is that a solid strip of chrome -- the top
    HUD, when hints are on -- can no longer be dragged through, which is
    how a toolbar behaves everywhere else.

    `chooser._Surface` is this same fix on the pre-snip chooser (SNX-108);
    together they are the whole of the overlay's own chrome.
    """

    def mousePressEvent(self, event) -> None:
        event.accept()


def _bar_font() -> QFont:
    """The split action's label font."""
    font = QFont(design.font_families().ui)
    size, weight = design.tokens.BarFont.SPLIT
    font.setPixelSize(round(size))
    font.setWeight(QFont.Weight(weight))
    return font


class _SplitAction(QWidget):
    """The bar's primary action: a destination on the face, and a caret
    that offers the other two.

    The handoff's stills bar leads with one of these rather than a row of
    equal icon buttons -- "the chooser sets the split button's face; the
    chevron always offers the other two", so the destination picked before
    the snip is the one already under the cursor, and changing your mind
    costs a menu rather than a hunt.

    Two hit areas, one control: pressing the face fires the destination,
    pressing the caret opens the menu. The seam between them is drawn, so
    which half a click will land in is visible before the click.
    """

    activated = pyqtSignal(str)
    menuRequested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._destination = "Copy"
        self._icon_name = "copy"
        self._hovered_half = None
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(design.tokens.BarMetric.BTN)
        self._relayout()

    def destination(self) -> str:
        return self._destination

    def set_destination(self, destination: str, icon_name: str) -> None:
        self._destination = destination
        self._icon_name = icon_name
        self._relayout()

    def _relayout(self) -> None:
        metric = design.tokens.BarMetric
        text = QFontMetricsF(_bar_font()).horizontalAdvance(self._destination)
        # Rounded up: a face a fraction of a pixel narrow clips the label's
        # last glyph, and the fallback faces this runs in are measured, not
        # the Plex the handoff's widths assume.
        self._face_w = math.ceil(
            metric.SPLIT_PAD_H + metric.SPLIT_ICON + metric.SPLIT_GAP + text + metric.SPLIT_PAD_H
        )
        # The seam is a pixel of its own between the halves, as the spec
        # draws it, rather than a line painted over the face's edge.
        self.setFixedWidth(self._face_w + 1 + metric.SPLIT_CARET_W)
        self.updateGeometry()
        self.update()

    def _half_at(self, x: float) -> str:
        return "face" if x < self._face_w else "caret"

    def mouseMoveEvent(self, event) -> None:
        half = self._half_at(event.position().x())
        if half != self._hovered_half:
            self._hovered_half = half
            self.update()

    def leaveEvent(self, event) -> None:
        self._hovered_half = None
        self.update()

    def mousePressEvent(self, event) -> None:
        event.accept()

    def mouseReleaseEvent(self, event) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            return
        if not self.rect().contains(event.position().toPoint()):
            return
        if self._half_at(event.position().x()) == "caret":
            self.menuRequested.emit()
        else:
            self.activated.emit(self._destination)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        metric = design.tokens.BarMetric
        radius = metric.BTN_RADIUS
        caret_left = self._face_w + 1

        shape = QPainterPath()
        shape.addRoundedRect(QRectF(self.rect()), radius, radius)
        fill = design.bar_color("ACCENT")
        painter.fillPath(shape, fill)
        if self._hovered_half is not None:
            # Only the half under the pointer brightens: the two halves do
            # different things, so hover has to say which one is live.
            half = (
                QRectF(0, 0, self._face_w, self.height())
                if self._hovered_half == "face"
                else QRectF(caret_left, 0, self.width() - caret_left, self.height())
            )
            clip = QPainterPath()
            clip.addRect(half)
            painter.fillPath(shape.intersected(clip), fill.lighter(106))

        text_colour = design.bar_color("ACCENT_FG")
        x = float(metric.SPLIT_PAD_H)
        size = metric.SPLIT_ICON
        pixmap = design.icon(self._icon_name, text_colour).pixmap(size, size)
        painter.drawPixmap(round(x), round((self.height() - size) / 2), pixmap)
        x += size + metric.SPLIT_GAP

        painter.setFont(_bar_font())
        painter.setPen(text_colour)
        painter.drawText(
            QRectF(x, 0, self._face_w - x, self.height()),
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            self._destination,
        )

        # The seam: which half a click lands in has to be visible before
        # the click, or a split button is just a button that sometimes does
        # something else.
        painter.fillRect(
            QRectF(self._face_w, 0, 1, self.height()), design.bar_color("SPLIT_SEAM")
        )

        caret = metric.CHEVRON
        pixmap = design.icon("chevron", text_colour).pixmap(caret, caret)
        painter.drawPixmap(
            round(caret_left + (metric.SPLIT_CARET_W - caret) / 2),
            round((self.height() - caret) / 2),
            pixmap,
        )
        painter.end()


class _StyleDot(QWidget):
    """The style slot: one button that is its own preview -- a dot in the
    active tool's colour at its stroke's diameter, or a ring in that colour
    for a shape that is outline only -- so colour, stroke and fill read
    without opening anything. A redaction has no colour, so its dot is the
    bar's own idle grey, at its strength.

    It opens the style popover. A tool nothing on it can change
    (`tokens.UNSTYLED_TOOLS`) dims it and it stops opening, because a
    control that can do nothing should not look live. That dim is real
    opacity -- one of the two places the handoff allows it -- painted here
    rather than put on the widget as an effect.
    """

    clicked = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        metric = design.tokens.BarMetric
        self.setFixedSize(metric.BTN, metric.BTN)
        self.setMouseTracking(True)
        self._style = ToolStyle(
            colour=design.tokens.INK_SWATCHES[0][1], size=design.tokens.Metric.STROKE_DEFAULT
        )
        self._tool: str | None = None
        self._open = False
        self._refresh()

    @property
    def is_stylable(self) -> bool:
        return self._tool is not None and self._tool not in design.tokens.UNSTYLED_TOOLS

    @property
    def is_open(self) -> bool:
        return self._open

    @property
    def style(self) -> ToolStyle:
        return self._style

    def _sections(self) -> list[str]:
        return design.tokens.STYLE_SECTIONS.get(self._tool, [])

    @property
    def is_ring(self) -> bool:
        """Outline only: the fill is part of what the dot has to show."""
        return "fill" in self._sections() and self._style.fill == "outline"

    @property
    def diameter(self) -> float:
        metric = design.tokens.BarMetric
        scale = (
            metric.STYLE_DOT_SCALE_HIGHLIGHTER
            if self._tool == "highlighter"
            else metric.STYLE_DOT_SCALE
        )
        value = self._style.strength if "strength" in self._sections() else self._style.size
        return max(metric.STYLE_DOT_MIN, min(value * scale, metric.STYLE_DOT_MAX))

    def set_style(self, style: ToolStyle) -> None:
        self._style = style
        self._refresh()

    def set_tool(self, tool: str | None) -> None:
        self._tool = tool
        self._refresh()

    def set_open(self, open_: bool) -> None:
        self._open = open_
        self.update()

    def _refresh(self) -> None:
        tool = self._tool
        name = design.tokens.TOOL_NAMES.get(tool, _tool_label(tool)) if tool else ""
        if tool is None:
            tooltip = "Style — pick a tool first"
        elif not self.is_stylable:
            tooltip = f"{name} has nothing to style"
        elif "strength" in self._sections():
            tooltip = f"{name} · strength {self._style.strength} — click for style"
        else:
            colour = QColor(self._style.colour).name()
            tooltip = f"{name} · {colour} · {self._style.size}px — click for style"
        self.setToolTip(tooltip)
        self.setCursor(
            Qt.CursorShape.PointingHandCursor if self.is_stylable else Qt.CursorShape.ArrowCursor
        )
        self.update()

    def mousePressEvent(self, event) -> None:
        # Accepted even when it will not open: the frame under the bar reads
        # an unhandled press as the start of a drag.
        event.accept()

    def mouseReleaseEvent(self, event) -> None:
        if (
            event.button() == Qt.MouseButton.LeftButton
            and self.is_stylable
            and self.rect().contains(event.position().toPoint())
        ):
            self.clicked.emit()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        metric = design.tokens.BarMetric
        if not self.is_stylable:
            painter.setOpacity(design.tokens.BarColor.DISABLED_OPACITY)

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(design.bar_color("STYLE_DOT_BG_OPEN" if self._open else "STYLE_DOT_BG"))
        painter.drawRoundedRect(QRectF(self.rect()), metric.BTN_RADIUS, metric.BTN_RADIUS)

        centre = QRectF(self.rect()).center()
        radius = self.diameter / 2
        colour = (
            design.bar_color("TOOL_IDLE_FG")
            if "strength" in self._sections()
            else QColor(self._style.colour)
        )
        if self.is_ring:
            # The spec's inset ring: drawn inside the diameter, so an outline
            # and a filled shape at the same stroke read the same size.
            width = metric.STYLE_DOT_OUTLINE
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(colour, width))
            painter.drawEllipse(centre, radius - width / 2, radius - width / 2)
            painter.end()
            return
        # The ring sits outside the dot, so a dot in a colour close to the
        # bar's own still has an edge.
        ring = radius + metric.STYLE_DOT_RING
        painter.setBrush(design.bar_color("STYLE_DOT_RING"))
        painter.drawEllipse(centre, ring, ring)
        painter.setBrush(colour)
        painter.drawEllipse(centre, radius, radius)
        painter.end()


class FloatingBar(_Chrome):
    """The stills bar: one row under the selection, per
    docs/design/bars/README.md section 2 -- the destination at the left end,
    seven tool slots in a fixed order of consequence, the style dot, then
    undo and clear.

    Two of the slots hold families (`tokens.FAMILIES`): shapes and
    redaction. A family slot shows whichever sibling was used last, a notch
    in its corner asks for the family's menu, and each sibling keeps its own
    shortcut. The menu itself is the hosting window's, as is the popover the
    style dot opens: they are chrome over that window, not part of this row
    (see `FamilyMenu`).

    A real child widget of the window it sits over, built from real buttons
    -- never painted inside that window's paintEvent -- which is what gives
    every control a tooltip and hover state for free. `paintEvent` paints the
    glass as a translucent *brush*, not a reduced-*opacity* widget:
    `setWindowOpacity` would dim every glyph along with the background.

    The bar can be dragged by its own surface -- the padding round the row,
    the gaps between controls and the dividers, which is everywhere a press
    used to do nothing -- once `reposition` has placed it over a selection
    (#50, divergences.md 9). A press on a control is that control's, so a
    click can never turn into a drag. The review window lays its bar out
    itself and never calls `reposition`, so its bar does not drag: a drag
    has nothing to be clamped to until then.
    """

    # A drag of the bar itself crossed the drag threshold, and moved the
    # bar. What hangs off the bar is the host's, so it follows on these
    # rather than the bar reaching for it.
    dragStarted = pyqtSignal()
    dragMoved = pyqtSignal()
    # A drag ended somewhere the bar should go again the next time a
    # selection leaves it no room: the `spot` it ended on, as (x, y).
    spotRemembered = pyqtSignal(float, float)

    toolSelected = pyqtSignal(str)
    # A tool picked with the pointer on this row, emitted just before the
    # `toolSelected` it causes. Clicking a tool closes whatever menu is open;
    # a key that changes the tool does not have to, and only this tells the
    # two apart.
    toolPicked = pyqtSignal(str)
    # "shapes" or "redact": the notch was pressed, or the slot right-clicked.
    familyMenuRequested = pyqtSignal(str)
    styleRequested = pyqtSignal()
    undoRequested = pyqtSignal()
    redoRequested = pyqtSignal()
    clearRequested = pyqtSignal()
    copyRequested = pyqtSignal()
    saveRequested = pyqtSignal()
    openRequested = pyqtSignal()
    destinationMenuRequested = pyqtSignal()
    captureChipClicked = pyqtSignal()
    # Which tool the cursor is over, so a window can name it without relying
    # on Qt's tooltip timer -- see `ToolHintStrip`.
    toolHovered = pyqtSignal(str)
    toolUnhovered = pyqtSignal()

    UNDO_SHORTCUT = "Ctrl+Z"
    REDO_SHORTCUT = "Ctrl+Shift+Z"

    # The spec's own wording for each family's two ways in.
    _NOTCH_TOOLTIPS = {"shapes": "Choose a shape", "redact": "Redaction mode"}
    _FAMILY_TOOLTIP_TAILS = {"shapes": "notch for more shapes", "redact": "notch to switch"}

    def __init__(self, parent=None, *, capture_chip: bool = True, trailing: str = "save"):
        """`capture_chip` and `trailing` exist for the review window, which
        instantiates this very widget rather than growing a second one --
        see `docs/design/handoff-windows.md`, "Annotate mode".

        There is nothing left to capture in that window, so its bar has no
        capture-mode chip; and its footer already owns Copy and Save As, so
        its trailing action is `Done` instead. Those are the only two
        differences the design allows, which is why they are the only two
        parameters.
        """
        super().__init__(parent)
        self._has_capture_chip = capture_chip
        self._trailing = trailing
        # The widget's own backdrop is transparent so paintEvent's alpha
        # fill is the only thing establishing a background colour --
        # without this attribute Qt composites the widget as opaque and the
        # "94% alpha, not 94% opacity" distinction has nothing to paint
        # against.
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

        metric = design.tokens.BarMetric
        layout = QHBoxLayout(self)
        inset = metric.PAD + metric.BORDER
        layout.setContentsMargins(inset, inset, inset, inset)
        layout.setSpacing(metric.GAP)

        self._active_tool: str | None = None
        # The sibling each family slot shows, and arms when clicked: the one
        # used last, starting from the first.
        self._family_choice = {
            family: siblings[0] for family, siblings in design.tokens.FAMILIES.items()
        }
        self._tool_buttons: dict[str, _IconButton] = {}
        # SNX-68: the last (selection, bounds) pair handed to `reposition`
        # -- `set_capture_mode` replays them through it whenever the chip's
        # label changes width, so the bar re-centres itself instead of just
        # growing from its own top-left corner. `None` until the first
        # `reposition` call, which is what lets a bare `FloatingBar()` fall
        # back to a plain resize.
        self._last_selection: QRect | None = None
        self._last_bounds: QRectF | None = None

        # A press on the bar's own surface that may become a drag (#50):
        # where it landed and where the bar's top-left was then, both in the
        # parent's logical coordinates. The bar's own coordinates move with
        # the bar, so a drag measured in them would chase itself. None while
        # no press is open.
        self._drag_press: QPointF | None = None
        self._drag_origin: QPoint | None = None
        self._dragging = False
        # Where a drag put the bar for the selection it was made over, as a
        # `spot` -- kept until that selection changes, so a re-sync for the
        # same selection does not snap the bar back.
        self._dragged_spot: tuple[float, float] | None = None
        self._dragged_for: QRect | None = None
        # Where the bar goes when neither side of a selection has room, as
        # a `spot`, or None for the automatic answer.
        self._remembered_spot: tuple[float, float] | None = None

        # The primary action sits at the LEFT end, before a divider, and
        # picking a tool never changes anything to its left. It used to
        # trail the bar, so the one control that finishes the snip was the
        # last thing read and moved every time the tool group changed width.
        #
        # The overlay leads with a split button -- "the chooser sets the
        # split button's face; the chevron always offers the other two" --
        # so the destination chosen before the snip is already under the
        # cursor. The review window keeps its pair: its footer already owns
        # the exports, and a second destination control on the bar above
        # them would be two answers to one question.
        self._action: _SplitAction | None = None
        self._copy_button: _IconButton | None = None
        self._save_button: _PillButton | None = None
        if self._trailing == "done":
            self._copy_button = _IconButton(
                "copy", "Copy", idle_color=design.color("ICON_NEUTRAL")
            )
            self._copy_button.clicked.connect(self.copyRequested)
            layout.addWidget(self._copy_button)

            self._save_button = self._build_save_button()
            self._save_button.clicked.connect(self.saveRequested)
            layout.addWidget(self._save_button)
        else:
            self._action = _SplitAction(self)
            self._action.activated.connect(self._on_destination_activated)
            self._action.menuRequested.connect(self.destinationMenuRequested)
            layout.addWidget(self._action)
        self._add_divider(layout)

        # The chip is built but not placed on the overlay's bar: the
        # handoff's post-selection bar carries no mode control, and the way
        # back to one is Space, which reopens the chooser with the mode
        # still on it.
        self._chip = self._build_capture_chip()
        self._chip.clicked.connect(self.captureChipClicked)
        if capture_chip and self._trailing == "done":
            layout.addWidget(self._chip)
            self._add_divider(layout)
        else:
            self._chip.hide()

        for slot in design.tokens.STILLS_SLOTS:
            tool = self._family_choice.get(slot, slot)
            button = _IconButton(_tool_glyph(tool), self._slot_tooltip(slot, tool), name=tool)
            button.clicked.connect(lambda checked=False, s=slot: self._on_slot_clicked(s))
            if slot in design.tokens.FAMILIES:
                notch = button.add_notch(self._NOTCH_TOOLTIPS[slot])
                notch.clicked.connect(lambda s=slot: self.familyMenuRequested.emit(s))
                button.rightClicked.connect(lambda s=slot: self.familyMenuRequested.emit(s))
            button.hovered.connect(self.toolHovered)
            button.unhovered.connect(self.toolUnhovered)
            self._tool_buttons[slot] = button
            layout.addWidget(button)
        self._add_divider(layout)

        # The watermark joins the style dot here, as a slot of its own.
        self._style_dot = _StyleDot(self)
        self._style_dot.clicked.connect(self.styleRequested)
        layout.addWidget(self._style_dot)
        self._add_divider(layout)

        self._undo_button = _IconButton("undo", f"Undo — {self.UNDO_SHORTCUT}")
        self._undo_button.clicked.connect(self.undoRequested)
        self._undo_button.setEnabled(False)
        layout.addWidget(self._undo_button)

        # Undo and clear only, per the handoff's bar. Redo keeps its
        # keyboard shortcut and its place in the review window; on the
        # overlay it was a button pressed far less often than the tools it
        # was widening the bar for.
        self._redo_button = _IconButton("redo", f"Redo — {self.REDO_SHORTCUT}")
        self._redo_button.clicked.connect(self.redoRequested)
        self._redo_button.setEnabled(False)
        if self._trailing == "done":
            layout.addWidget(self._redo_button)
        else:
            self._redo_button.hide()

        self._clear_button = _IconButton(
            "trash",
            "Clear ink",
            hover_bg=design.bar_color("DANGER_BG"),
            hover_color=design.bar_color("DANGER_FG"),
        )
        self._clear_button.clicked.connect(self.clearRequested)
        layout.addWidget(self._clear_button)

    def set_destination(self, destination: str) -> None:
        """Put `destination` on the split button's face.

        A no-op on the review window's bar, which has no split button --
        its footer owns the exports.
        """
        if self._action is None:
            return
        icon = {"Copy": "copy", "Save": "save", "Open": "eye"}.get(destination, "copy")
        self._action.set_destination(destination, icon)

    def destination(self) -> str:
        return self._action.destination() if self._action is not None else "Copy"

    def _on_destination_activated(self, destination: str) -> None:
        {
            "Copy": self.copyRequested,
            "Save": self.saveRequested,
            "Open": self.openRequested,
        }.get(destination, self.copyRequested).emit()

    # -- construction helpers ------------------------------------------------

    def _add_divider(self, layout: QHBoxLayout) -> None:
        metric = design.tokens.BarMetric
        layout.addSpacing(metric.DIVIDER_MARGIN)
        layout.addWidget(
            _Divider(self, height=metric.DIVIDER_H, colour=design.bar_color("DIVIDER"))
        )
        layout.addSpacing(metric.DIVIDER_MARGIN)

    def _slot_tooltip(self, slot: str, tool: str) -> str:
        tail = self._FAMILY_TOOLTIP_TAILS.get(slot)
        return f"{_tool_tooltip(tool)} · {tail}" if tail else _tool_tooltip(tool)

    def _build_capture_chip(self) -> _PillButton:
        label, _icon, _note = design.tokens.CAPTURE_MODES[0]  # "Region", the bar's default
        metric = design.tokens.Metric
        chip = _PillButton(
            "chevron",
            label,
            icon_size=14,
            # Neutral, not accent: the primary action is the *only*
            # accent-filled control in the bar.
            text_color=design.color("TEXT_PRIMARY"),
            bg_color=design.color("BAR_BORDER"),
            icon_after=True,
            pad_left=metric.CHIP_PAD_L,
            pad_right=metric.CHIP_PAD_R,
            tooltip="Capture mode",
        )
        chip.setFixedHeight(design.tokens.BarMetric.BTN)
        return chip

    def _build_save_button(self) -> _PillButton:
        """The review window's primary action, `Done`: its footer already
        owns the exports.

        It leads the bar rather than trailing it, and carries the accent --
        the one control in the bar that may.
        """
        metric = design.tokens.BarMetric
        done = self._trailing == "done"
        button = _PillButton(
            "check" if done else "save",
            "Done" if done else "Save",
            icon_size=metric.SPLIT_ICON,
            text_color=design.bar_color("ACCENT_FG"),
            bg_color=design.bar_color("ACCENT"),
            icon_after=False,
            pad_left=metric.SPLIT_PAD_H,
            pad_right=metric.SPLIT_PAD_H,
            tooltip="Done" if done else "Save",
        )
        button.setFixedHeight(metric.BTN)
        return button

    # -- capture mode (SNX-44) --------------------------------------------

    def set_capture_mode(self, label: str) -> None:
        """Update the chip's own label to `label`, and re-centre the bar for
        its new width.

        SNX-68: `_PillButton.sizeHint` (SNX-59) already measures the new
        label correctly, but nothing re-read it after construction, so the
        bar itself stayed at its old width and clipped whichever label
        didn't fit in it. Replaying the last `reposition` call redoes both
        the sizing *and* the centring in one place. Falls back to a plain
        resize when `reposition` was never called at all -- a bare
        `FloatingBar()` with no selection yet.
        """
        self._chip.set_text(label)
        if self._last_selection is not None and self._last_bounds is not None:
            self.reposition(self._last_selection, self._last_bounds)
        else:
            self.resize(self.sizeHint())

    # -- tools -------------------------------------------------------------

    def _on_slot_clicked(self, slot: str) -> None:
        """Arm the slot's tool: for a family, the sibling it is showing.

        A click never moves on to the next sibling. The notch is how to
        reach another, and each has its key; a click that sometimes changed
        what the slot draws would make the one gesture used most the one
        that cannot be trusted.
        """
        tool = self._family_choice.get(slot, slot)
        self.toolPicked.emit(tool)
        self.select_tool(tool)

    def select_tool(self, tool: str) -> None:
        """Make `tool` the active tool, the one place that happens -- a
        click, a family menu pick and a shortcut key all arrive here, so
        none of them is a copy of the others that could drift.

        A family sibling also becomes the one its slot shows, and so the
        one a plain click on that slot arms next.
        """
        family = _family_of(tool)
        if family is not None:
            self._family_choice[family] = tool
            button = self._tool_buttons[family]
            button.set_glyph(tool, _tool_glyph(tool))
            button.setToolTip(self._slot_tooltip(family, tool))
        self.set_active_tool(tool)
        self.toolSelected.emit(tool)

    def handle_tool_key(self, key: int) -> bool:
        """Arm whatever `key` reaches, as `select_tool` would, and say
        whether it was a tool key at all. The redaction family's one key
        cycles its siblings.
        """
        tool = _SHORTCUT_KEY_CODES.get(key)
        if tool is not None:
            self.select_tool(tool)
            return True
        if key == _REDACTION_KEY_CODE:
            self.cycle_redaction()
            return True
        return False

    def cycle_redaction(self) -> None:
        """The redaction key: arm the sibling the slot shows, or, when a
        redaction tool is already armed, the next one after it.
        """
        siblings = design.tokens.FAMILIES["redact"]
        tool = self._family_choice["redact"]
        if self._active_tool in siblings:
            tool = siblings[(siblings.index(self._active_tool) + 1) % len(siblings)]
        self.select_tool(tool)

    def family_choice(self, family: str) -> str:
        """The sibling `family`'s slot shows."""
        return self._family_choice[family]

    def set_active_tool(self, tool: str | None) -> None:
        """Mark `tool`'s slot active and every other slot idle -- a family
        slot reads active for any of its siblings.
        """
        self._active_tool = tool
        family = _family_of(tool)
        for slot, button in self._tool_buttons.items():
            button.set_active(slot in (tool, family))
        self._style_dot.set_tool(tool)

    @property
    def active_tool(self) -> str | None:
        return self._active_tool

    def slot_rect(self, slot: str, host: QWidget) -> QRect:
        """`slot`'s button, in `host`'s coordinates -- where a menu for it is
        anchored.
        """
        button = self._tool_buttons[slot]
        return QRect(button.mapTo(host, QPoint(0, 0)), button.size())

    # -- style -------------------------------------------------------------

    def set_style_preview(self, style: ToolStyle) -> None:
        """What the style dot shows: the active tool's style."""
        self._style_dot.set_style(style)

    def set_style_open(self, open_: bool) -> None:
        self._style_dot.set_open(open_)

    def style_dot_rect(self, host: QWidget) -> QRect:
        """The style dot, in `host`'s coordinates -- where its popover is
        anchored.
        """
        return QRect(self._style_dot.mapTo(host, QPoint(0, 0)), self._style_dot.size())

    # -- undo / redo -------------------------------------------------------

    def set_undo_enabled(self, enabled: bool) -> None:
        self._undo_button.setEnabled(enabled)

    def set_redo_enabled(self, enabled: bool) -> None:
        self._redo_button.setEnabled(enabled)

    # -- fill ----------------------------------------------------------------

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        metric = design.tokens.BarMetric
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)

        # design.bar_color("BAR_BG") already carries its 94% alpha --
        # painted here as a translucent *fill*, never as reduced *widget*
        # opacity, so every child painted after this stays fully opaque.
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(design.bar_color("BAR_BG"))
        painter.drawRoundedRect(rect, metric.RADIUS, metric.RADIUS)

        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(design.bar_color("BAR_BORDER"))
        painter.drawRoundedRect(rect, metric.RADIUS, metric.RADIUS)
        painter.end()

    # -- dragging (#50) ----------------------------------------------------

    @property
    def is_dragging(self) -> bool:
        """A press on the bar's own surface is open, whether or not it has
        moved far enough to be a drag yet.
        """
        return self._drag_press is not None

    def set_remembered_spot(self, spot: "tuple[float, float] | None") -> None:
        """Where the bar goes when a selection leaves it no room on either
        side, as a `spot`; None leaves that to `placement`.
        """
        self._remembered_spot = spot

    def _is_grip(self, pos: QPointF) -> bool:
        """Whether a press at `pos` (this bar's own coordinates) takes hold
        of the bar rather than of a control.

        Asked of the widget under the press rather than left to which
        widget accepts it. A press reaches the bar whenever the widget under
        the pointer declines it, and a label or a plainly painted widget
        declines every press -- so a slot built from one would be a grip in
        the middle of the row. Only the bar's own surface and its dividers
        take hold of it, whatever the row comes to hold.
        """
        child = self.childAt(pos.toPoint())
        return child is None or isinstance(child, _Divider)

    def mousePressEvent(self, event) -> None:
        # Accepted whatever happens next, as every piece of chrome's press
        # is -- see `_Chrome`.
        event.accept()
        # A press while one is still open means its release was lost.
        self.end_drag()
        if (
            event.button() != Qt.MouseButton.LeftButton
            or self._last_bounds is None
            or not self._is_grip(event.position())
        ):
            return
        self._drag_press = self.mapToParent(event.position())
        self._drag_origin = self.pos()

    def mouseMoveEvent(self, event) -> None:
        if self._drag_press is None or self._last_bounds is None:
            return
        if not event.buttons() & Qt.MouseButton.LeftButton:
            self.end_drag()
            return
        travel = self.mapToParent(event.position()) - self._drag_press
        if not self._dragging:
            # A press on the padding is usually a click that missed a
            # button by a pixel or two, and must leave the bar where it is.
            if travel.manhattanLength() < QApplication.startDragDistance():
                return
            self._dragging = True
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            self.dragStarted.emit()
        self.move(self.clamped(QPointF(self._drag_origin) + travel, self._last_bounds, self.size()))
        self.dragMoved.emit()

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.end_drag()

    def end_drag(self) -> None:
        """End an open press on the bar, leaving the bar where its last move
        put it.

        A release is only one way that happens. The host calls this too for
        a move with no button held, a press elsewhere and the window losing
        focus -- the same guards `OverlayWindow._end_drags` keeps for a
        mark, so a lost release cannot leave the bar following the pointer.

        A drag made while the selection has no room beside it is
        remembered (`spotRemembered`). One made beside a selection with room
        is kept for that selection only: it moved the bar for this snip, and
        remembering it would change where the bar goes for a whole-monitor
        snip that it was never about.
        """
        dragged = self._dragging
        self._drag_press = None
        self._drag_origin = None
        self._dragging = False
        if not dragged or self._last_bounds is None or self._last_selection is None:
            return
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        spot = self.spot(QPointF(self.pos()), self._last_bounds, self.size())
        self._dragged_spot = spot
        self._dragged_for = QRect(self._last_selection)
        if self._beside(QRectF(self._last_selection), self._last_bounds, self.size()) is None:
            self._remembered_spot = spot
            self.spotRemembered.emit(*spot)

    # -- positioning -----------------------------------------------------

    def reposition(self, selection: QRect, bounds: QRectF) -> None:
        """Put the bar where it belongs for `selection`: where a drag put it
        for this same selection, and otherwise where `placement` says.

        Kept apart from `placement` so the rule and applying it stay two
        things: a position the user chose replaces the one, not the other.
        """
        if self._dragged_for is not None and QRect(selection) != self._dragged_for:
            self._dragged_spot = None
            self._dragged_for = None
        # SNX-68: remembered so `set_capture_mode` can replay this same
        # call when the chip's width changes.
        self._last_selection = selection
        self._last_bounds = bounds
        # Placed over a selection, the bar has bounds a drag can be clamped
        # to, and its surface says it can be taken hold of. The controls
        # keep their own pointing hand.
        self.setCursor(
            Qt.CursorShape.ClosedHandCursor if self._dragging else Qt.CursorShape.OpenHandCursor
        )
        if self._dragging:
            # Mid-drag the pointer owns the bar's position.
            return
        size = self.sizeHint()
        if self._dragged_spot is not None:
            top_left = self.from_spot(self._dragged_spot, bounds, size)
        else:
            top_left = self.placement(QRectF(selection), bounds, size, self._remembered_spot)
        self.setGeometry(QRect(top_left, size))

    @staticmethod
    def placement(
        selection: QRectF,
        bounds: QRectF,
        size: QSize,
        remembered: "tuple[float, float] | None" = None,
    ) -> QPoint:
        """Where a bar of `size` belongs for `selection`: its top-left, in the
        space `selection` and `bounds` share.

        `bounds` is the selection's own monitor, less whatever the desktop
        reserves on it (`OverlayWindow._chrome_bounds`) -- never the window.
        On a desk whose monitors are staggered vertically, clamping to the
        window put the bar for a selection low on a short monitor into the
        gap below it: inside the window, on no monitor at all.

        Centred on the selection, `BAR_OFFSET_Y` below it, and at least
        `BAR_EDGE_MARGIN` inside `bounds` on every side.

        Where there is no room below, the bar goes above the selection
        rather than being clamped back up over it, as the handoff's
        `BAR_BOTTOM_ROOM` would put it (divergences.md 8). That clamp covers
        the very pixels the user framed in order to mark them up -- reported
        as "when u select a small region the controls are in the region so
        u cant edit anything", on a 1123x74 strip. Height is not what
        decides it; distance from the monitor's bottom edge is.

        With room on neither side the selection is essentially the whole
        monitor, and anywhere covers some of it (#50). The bar goes to the
        `remembered` spot the user last dragged it to in that case, and
        with none it is held inside `bounds` against its bottom margin.
        The remembered spot is only ever the answer to that case: while
        there is room beside the selection it is not consulted, so it can
        never put the bar over a selection that had somewhere else to put
        it.
        """
        metric = design.tokens.BarMetric
        margin = metric.BAR_EDGE_MARGIN
        top = FloatingBar._beside(selection, bounds, size)
        if top is None:
            if remembered is not None:
                return FloatingBar.from_spot(remembered, bounds, size)
            top = max(bounds.top() + margin, bounds.bottom() - margin - size.height())
        left = selection.center().x() - size.width() / 2
        left = max(bounds.left() + margin, min(left, bounds.right() - margin - size.width()))
        return QPoint(round(left), round(top))

    @staticmethod
    def _beside(selection: QRectF, bounds: QRectF, size: QSize) -> "float | None":
        """The top that puts a bar of `size` beside `selection` inside
        `bounds` -- below it, or above it when below does not fit -- or None
        when neither side has room.
        """
        metric = design.tokens.BarMetric
        below = selection.bottom() + metric.BAR_OFFSET_Y
        if below <= bounds.bottom() - metric.BAR_EDGE_MARGIN - size.height():
            return below
        above = selection.top() - metric.BAR_OFFSET_Y - size.height()
        if above >= bounds.top() + metric.BAR_EDGE_MARGIN:
            return above
        return None

    @staticmethod
    def _travel(bounds: QRectF, size: QSize) -> QRectF:
        """Every top-left that keeps a bar of `size` `BAR_EDGE_MARGIN` inside
        `bounds`, as a rect of top-left points in `bounds`' own space.

        Empty along an axis the bar does not fit on at all, and pinned to
        that axis's near margin, which is where `placement` has always put a
        bar wider than its monitor.
        """
        margin = design.tokens.BarMetric.BAR_EDGE_MARGIN
        return QRectF(
            bounds.left() + margin,
            bounds.top() + margin,
            max(0.0, bounds.width() - 2 * margin - size.width()),
            max(0.0, bounds.height() - 2 * margin - size.height()),
        )

    @staticmethod
    def clamped(top_left: QPointF, bounds: QRectF, size: QSize) -> QPoint:
        """`top_left` moved the least distance that keeps a bar of `size`
        `BAR_EDGE_MARGIN` inside `bounds`. Same space in and out.
        """
        travel = FloatingBar._travel(bounds, size)
        return QPoint(
            round(max(travel.left(), min(top_left.x(), travel.right()))),
            round(max(travel.top(), min(top_left.y(), travel.bottom()))),
        )

    @staticmethod
    def spot(top_left: QPointF, bounds: QRectF, size: QSize) -> "tuple[float, float]":
        """Where a bar of `size` at `top_left` sits in `bounds`, as a
        fraction of the room it can travel along each axis: (0, 0) is its
        top-left position and (1, 1) its bottom-right.

        That is the form a position is remembered in, because it means the
        same place on any monitor. Pixels -- absolute or monitor-local --
        point off a smaller monitor, or under a dock added since, and a
        fraction of the monitor itself would still push a bar remembered
        against the right edge off one narrower than it was. A fraction of
        the room the bar has is inside the usable area on every monitor by
        construction, whatever the bar's own width has become.

        The middle, 0.5, along an axis the bar cannot move on at all.
        """
        travel = FloatingBar._travel(bounds, size)

        def fraction(offset: float, room: float) -> float:
            return 0.5 if room <= 0 else max(0.0, min(offset / room, 1.0))

        return (
            fraction(top_left.x() - travel.left(), travel.width()),
            fraction(top_left.y() - travel.top(), travel.height()),
        )

    @staticmethod
    def from_spot(spot: "tuple[float, float]", bounds: QRectF, size: QSize) -> QPoint:
        """The top-left, in `bounds`' own space, that `spot` names for a bar
        of `size` -- the inverse of `spot`, clamped on the way in so a
        value from anywhere lands inside `bounds`.
        """
        travel = FloatingBar._travel(bounds, size)
        x, y = (max(0.0, min(float(value), 1.0)) for value in spot)
        return FloatingBar.clamped(
            QPointF(travel.left() + x * travel.width(), travel.top() + y * travel.height()),
            bounds,
            size,
        )


# ---------------------------------------------------------------------------
# The tool hint strip
# ---------------------------------------------------------------------------
# What hangs under the bar while the style popover is closed: the active
# tool's name and what it does. It used to lead the colour-and-stroke tray,
# and it is the part of that tray that was always worth having up.


class _ToolPill(QWidget):
    """The tool hint strip's pill: a static (non-clickable) pill naming
    the active tool -- glyph + label, per the old tray spec's "Active-tool
    pill" bullet. A plain QWidget, not a QPushButton: nothing here is clickable,
    only its translucent fill and its two child QLabels change when the
    tool does.
    """

    # Not a tokens.Color entry -- no other control in the design uses this
    # exact 8% white fill, so it lives here as a class constant the same
    # way FloatingBar's own _TOP_MAX_FROM_BOTTOM keeps a README literal
    # that isn't a token.
    _BG_ALPHA = 0.08
    _RADIUS = 8
    _ICON_SIZE = 14

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

        # Padding "3/9/3/6" per the spec -- CSS top/right/bottom/left --
        # asymmetric because the glyph sits close to the pill's own rounded
        # left edge while the label needs more room on the right.
        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 3, 9, 3)
        layout.setSpacing(6)

        self._icon_label = QLabel(self)
        layout.addWidget(self._icon_label)

        self._text_label = QLabel(self)
        font = QFont(design.font_families().ui)
        size, weight = design.tokens.Font.TRAY_LABEL
        font.setPixelSize(round(size))
        font.setWeight(QFont.Weight(weight))
        self._text_label.setFont(font)
        self._text_label.setStyleSheet(f"color: {design.color('TEXT_PRIMARY').name()};")
        layout.addWidget(self._text_label)

    def set_tool(self, tool: str) -> None:
        pixmap = design.icon(_tool_glyph(tool), design.color("TEXT_PRIMARY")).pixmap(
            self._ICON_SIZE, self._ICON_SIZE
        )
        self._icon_label.setPixmap(pixmap)
        self._text_label.setText(_tool_label(tool))

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        bg = QColor("#ffffff")
        bg.setAlphaF(self._BG_ALPHA)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(bg)
        painter.drawRoundedRect(QRectF(self.rect()), self._RADIUS, self._RADIUS)
        painter.end()


class ToolHintStrip(_Chrome):
    """Names the active tool and says what it does.

    A glyph is not self-explanatory at 15px and a tooltip is a hover away
    at best: when only the old colour tray named a tool, the eraser -- with
    nothing to configure -- was the one tool nobody could identify. The
    strip is up for every tool whenever the bar is and the style popover is
    not: the pill, and the hint from `tokens.TOOL_HINTS`.
    """

    _BG_ALPHA = 0.93
    _RADIUS = 12

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 6, 12, 6)
        layout.setSpacing(9)

        self._pill = _ToolPill(self)
        layout.addWidget(self._pill)

        self._hint = QLabel(self)
        font = QFont(design.font_families().ui)
        size, weight = design.tokens.Font.TRAY_HINT
        font.setPixelSize(round(size))
        font.setWeight(QFont.Weight(weight))
        self._hint.setFont(font)
        self._hint.setStyleSheet(f"color: {design.color('TEXT_MUTED').name()};")
        layout.addWidget(self._hint)

    def set_tool(self, tool: str) -> None:
        self._pill.set_tool(tool)
        self._hint.setText(design.tokens.TOOL_HINTS.get(tool, ""))

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        bg = design.color("BAR_BG")
        bg.setAlphaF(self._BG_ALPHA)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(bg)
        painter.drawRoundedRect(QRectF(self.rect()), self._RADIUS, self._RADIUS)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(design.color("BAR_BORDER"))
        painter.drawRoundedRect(
            QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), self._RADIUS, self._RADIUS
        )
        painter.end()


# ---------------------------------------------------------------------------
# The style popover
# ---------------------------------------------------------------------------
# docs/design/bars/README.md, "Style dot + popover" and "11a is conditional on
# the keyboard", is the authority, and docs/design/bars/divergences.md
# overrides it wherever the two differ. One 216px popover over the style dot
# replaces the colour-and-stroke tray and the blur tray that used to hang
# under the bar. It renders only the sections the active tool can use
# (`tokens.STYLE_SECTIONS`), and every change goes into that tool's own style
# (`marks.ToolStyles`).
#
# The handoff is plain that one row with a popover is only better than a
# visible tray if the keyboard is behind it: 1-7 pick a swatch, [ and ] step
# the stroke, D cycles the line style, and a pick never closes the popover.
# So nothing in it takes the keyboard focus -- a focused slider swallows
# every one of those keys (`OverlayWindow._shortcuts_suppressed`) -- and it
# is a child of the window the bar sits over rather than a popup, for the
# reasons `FamilyMenu` gives.

# The swatch keys, by Qt key code: 1 is the first of `tokens.INK_SWATCHES`.
_SWATCH_KEY_CODES = {
    getattr(Qt.Key, f"Key_{number}"): number - 1
    for number in range(1, len(design.tokens.INK_SWATCHES) + 1)
}
# [ thinner, ] thicker -- or, for a redaction, weaker and stronger.
_STEP_KEY_CODES = {Qt.Key.Key_BracketLeft: -1, Qt.Key.Key_BracketRight: 1}
_LINE_STYLE_KEY_CODE = Qt.Key.Key_D
_LINE_STYLE_KEY = "D"
_STEP_KEYS = "[ ]"


def _cycle_step(cycle: list[tuple], name: str) -> tuple[tuple, tuple]:
    """The entry `name` is in a click-through `cycle`, and the one a click
    moves on to. A name the cycle does not know reads as its first entry.
    """
    names = [entry[0] for entry in cycle]
    index = names.index(name) if name in names else 0
    return cycle[index], cycle[(index + 1) % len(cycle)]


class _SwatchButton(QPushButton):
    """One ink swatch in the popover's colour row, and its key.

    A real `QPushButton` for its click handling and tooltip, with the colour
    and ring hand-painted. The spec rings the picked swatch outward -- "0 0 0
    2px #1a1c18, 0 0 0 3.5px #f1f3e8" -- and a child widget cannot paint past
    its own bounds, so the same two rings are drawn inward: the light ring at
    the edge, the dark gap inside it, the colour inside that.
    """

    def __init__(self, name: str, hex_colour: str, key: str, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self._colour = QColor(hex_colour)
        self._selected = False
        _fit_to_the_colour_row(self)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMouseTracking(True)
        self.setToolTip(f"{name} — {key}")
        self.setFlat(True)
        self.setStyleSheet("QPushButton { border: none; background: transparent; }")

    def sizeHint(self) -> QSize:
        # The row shares its width out between the swatches, so this only
        # has to be small enough never to push the row past the popover.
        return QSize(design.tokens.BarMetric.SWATCH_H // 2, design.tokens.BarMetric.SWATCH_H)

    def minimumSizeHint(self) -> QSize:
        return self.sizeHint()

    @property
    def hex_colour(self) -> str:
        return self._colour.name()

    @property
    def is_selected(self) -> bool:
        return self._selected

    def set_selected(self, selected: bool) -> None:
        self._selected = selected
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        metric = design.tokens.BarMetric
        rect = QRectF(self.rect())
        radius = metric.SWATCH_RADIUS

        if self._selected:
            painter.setBrush(design.bar_color("SWATCH_RING"))
            painter.drawRoundedRect(rect, radius, radius)
            ring = metric.SWATCH_RING
            painter.setBrush(design.bar_color("SWATCH_RING_GAP"))
            inner = max(radius - ring, 0)
            painter.drawRoundedRect(rect.adjusted(ring, ring, -ring, -ring), inner, inner)
            inset = ring + metric.SWATCH_RING_GAP
            painter.setBrush(self._colour)
            inner = max(radius - inset, 0)
            painter.drawRoundedRect(rect.adjusted(inset, inset, -inset, -inset), inner, inner)
        else:
            # The spec's border lies over the colour, not beside it, so a
            # swatch the colour of the popover still has an edge.
            painter.setBrush(self._colour)
            painter.drawRoundedRect(rect, radius, radius)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(design.bar_color("SWATCH_BORDER"), 1))
            painter.drawRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5), radius, radius)
        painter.end()


class _CustomColorButton(QPushButton):
    """The colour row's `+`: a swatch's box with a dashed border and a plus,
    that opens `QColorDialog`. Whether a colour picked here should join the
    swatches for good is still open on #63, so it does what it always has.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        metric = design.tokens.BarMetric
        _fit_to_the_colour_row(self)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMouseTracking(True)
        self.setToolTip("Custom colour")
        self.setFlat(True)
        border = design.bar_color("CUSTOM_BORDER")
        # One border in one state, so a stylesheet can draw it.
        self.setStyleSheet(
            "QPushButton { border: 1px dashed rgba(%d, %d, %d, %s);"
            " border-radius: %dpx; background: transparent; }"
            % (border.red(), border.green(), border.blue(), border.alphaF(), metric.SWATCH_RADIUS)
        )
        self.setIcon(design.icon("plus", design.bar_color("CUSTOM_FG")))
        self.setIconSize(QSize(metric.CUSTOM_ICON, metric.CUSTOM_ICON))

    def sizeHint(self) -> QSize:
        # `QPushButton`'s own hint is wide enough for placeholder text this
        # button never has, which would take the row's width from the
        # swatches.
        return QSize(design.tokens.BarMetric.SWATCH_H // 2, design.tokens.BarMetric.SWATCH_H)

    def minimumSizeHint(self) -> QSize:
        return self.sizeHint()


def _fit_to_the_colour_row(button: QPushButton) -> None:
    """The spec's `flex: 1`: every box in the colour row the same width, the
    row's width shared between them, and none of them taking keyboard focus.
    """
    button.setFixedHeight(design.tokens.BarMetric.SWATCH_H)
    button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
    button.setFocusPolicy(Qt.FocusPolicy.NoFocus)


class _CycleButton(QPushButton):
    """Fill or line: one click-through button that shows the state it is in.

    A click moves on to the next state, the way the chooser's destination and
    delay do -- one behaviour to learn, and what took the popover from three
    rows to two. The glyph is the state; the tooltip names it, and what a
    click changes it to.
    """

    def __init__(self, kind: str, parent=None):
        super().__init__(parent)
        metric = design.tokens.BarMetric
        self.kind = kind
        self._state = (
            design.tokens.FILL_CYCLE if kind == "fill" else design.tokens.DASH_CYCLE
        )[0][0]
        self._hovered = False
        self.setFixedSize(metric.CYCLE_W, metric.CYCLE_H)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMouseTracking(True)
        self.setFlat(True)
        self.setStyleSheet("QPushButton { border: none; background: transparent; }")

    @property
    def state(self) -> str:
        return self._state

    def set_state(self, state: str, tooltip: str) -> None:
        self._state = state
        self.setToolTip(tooltip)
        self.update()

    def enterEvent(self, event) -> None:
        self._hovered = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._hovered = False
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        metric = design.tokens.BarMetric
        rect = QRectF(self.rect())
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(design.bar_color("CYCLE_HOVER_BG" if self._hovered else "CYCLE_BG"))
        painter.drawRoundedRect(rect, metric.CYCLE_RADIUS, metric.CYCLE_RADIUS)
        if self.kind == "fill":
            self._paint_fill_glyph(painter, rect.center())
        else:
            self._paint_dash_glyph(painter, rect.center())
        painter.end()

    def _paint_fill_glyph(self, painter: QPainter, centre: QPointF) -> None:
        """A small box drawn the way the state fills a shape: an outline, a
        solid box, or an outline round a wash.
        """
        metric = design.tokens.BarMetric
        box = QRectF(0, 0, metric.FILL_GLYPH_W, metric.FILL_GLYPH_H)
        box.moveCenter(centre)
        radius = metric.FILL_GLYPH_RADIUS
        glyph = design.bar_color("CYCLE_GLYPH")
        if self._state != "outline":
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(
                glyph if self._state == "filled" else design.bar_color("CYCLE_GLYPH_WASH")
            )
            painter.drawRoundedRect(box, radius, radius)
        if self._state != "filled":
            border = metric.FILL_GLYPH_BORDER
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(glyph, border))
            # Inside the box, as a CSS border is.
            half = border / 2
            inner = max(radius - half, 0)
            painter.drawRoundedRect(box.adjusted(half, half, -half, -half), inner, inner)

    def _paint_dash_glyph(self, painter: QPainter, centre: QPointF) -> None:
        """A short line in the state's own dash pattern."""
        metric = design.tokens.BarMetric
        width = metric.DASH_GLYPH_STROKE
        pen = QPen(design.bar_color("CYCLE_GLYPH"), width)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        (_name, pattern, _label), _next = _cycle_step(design.tokens.DASH_CYCLE, self._state)
        if pattern:
            # The spec's SVG dash array is in pixels, a QPen's in widths.
            pen.setDashPattern([length / width for length in pattern])
        painter.setPen(pen)
        half = metric.DASH_GLYPH_W / 2
        painter.drawLine(
            QPointF(centre.x() - half, centre.y()), QPointF(centre.x() + half, centre.y())
        )


def _style_slider(bounds: tuple[int, int], parent: QWidget) -> QSlider:
    """A slider in the spec's look that never takes the keyboard focus: a
    focused slider would keep [ and ] and the swatch keys from the window.
    """
    metric = design.tokens.BarMetric
    slider = QSlider(Qt.Orientation.Horizontal, parent)
    slider.setRange(*bounds)
    slider.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    slider.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
    slider.setCursor(Qt.CursorShape.PointingHandCursor)
    slider.setMouseTracking(True)
    track = design.bar_color("SLIDER_TRACK")
    # A stylesheet counts in whole pixels, so the thumb's overhang either
    # side of the track rounds up, and the thumb is a pixel over the spec's.
    overhang = math.ceil((metric.SLIDER_THUMB - metric.SLIDER_TRACK) / 2)
    thumb = metric.SLIDER_TRACK + 2 * overhang
    slider.setStyleSheet(
        "QSlider { background: transparent; min-height: %dpx; }"
        " QSlider::groove:horizontal { height: %dpx; border-radius: %dpx;"
        " background: rgba(%d, %d, %d, %s); }"
        " QSlider::handle:horizontal { width: %dpx; margin: -%dpx 0;"
        " border-radius: %dpx; background: %s; }"
        % (
            thumb,
            metric.SLIDER_TRACK,
            metric.SLIDER_TRACK // 2,
            track.red(),
            track.green(),
            track.blue(),
            track.alphaF(),
            thumb,
            overhang,
            thumb // 2,
            design.bar_color("SLIDER_THUMB").name(),
        )
    )
    return slider


def _style_readout(min_width: int, parent: QWidget) -> QLabel:
    """A slider's mono number, wide enough that the row never reflows as it
    changes.
    """
    readout = QLabel(parent)
    readout.setMinimumWidth(min_width)
    readout.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
    font = QFont(design.font_families().mono)
    size, weight = design.tokens.BarFont.READOUT
    font.setPixelSize(round(size))
    font.setWeight(QFont.Weight(weight))
    readout.setFont(font)
    readout.setStyleSheet(f"color: {design.bar_color('READOUT_FG').name()};")
    return readout


class StylePopover(_Chrome):
    """The style dot's popover: the active tool's style, and only the parts
    of it that tool can use.

    Two rows at most. The seven swatches and `+`, across one row; then fill
    and line, each one click-through button, and a strength or a stroke
    slider with its readout. A section the tool cannot use is hidden, never
    shown and inert.

    It is also where a tool's style changes from the keyboard, open or
    closed (`handle_key`), so a key and a click can never disagree about
    what they did. Every change goes into the `marks.ToolStyles` it was
    given, and `styleChanged` names the tool it changed.
    """

    styleChanged = pyqtSignal(str)
    # Shown or hidden -- the style dot reads as pressed while it is open.
    openChanged = pyqtSignal(bool)

    SECTIONS = ("color", "fill", "dash", "strength", "size")

    def __init__(self, styles: ToolStyles, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        metric = design.tokens.BarMetric
        tokens = design.tokens
        self._styles = styles
        self._tool: str | None = None

        # Border-box, as the handoff insists: the width is the popover's
        # whole outside edge, padding and border included.
        self.setFixedWidth(metric.MENU_W_STYLE)
        layout = QVBoxLayout(self)
        inset_h = metric.STYLE_PAD_H + metric.BORDER
        inset_v = metric.STYLE_PAD_V + metric.BORDER
        layout.setContentsMargins(inset_h, inset_v, inset_h, inset_v)
        layout.setSpacing(metric.STYLE_ROW_GAP)
        self._layout = layout

        self._colour_row = QWidget(self)
        colours = QHBoxLayout(self._colour_row)
        colours.setContentsMargins(0, 0, 0, 0)
        colours.setSpacing(metric.SWATCH_GAP)
        self._swatch_buttons: dict[str, _SwatchButton] = {}
        for index, (name, hex_colour) in enumerate(tokens.INK_SWATCHES):
            button = _SwatchButton(name, hex_colour, str(index + 1), self._colour_row)
            button.clicked.connect(lambda checked=False, c=hex_colour: self._apply(colour=c))
            self._swatch_buttons[hex_colour] = button
            colours.addWidget(button)
        self._custom_button = _CustomColorButton(self._colour_row)
        self._custom_button.clicked.connect(self._pick_custom_colour)
        colours.addWidget(self._custom_button)
        layout.addWidget(self._colour_row)

        self._controls_row = QWidget(self)
        controls = QHBoxLayout(self._controls_row)
        controls.setContentsMargins(0, 0, 0, 0)
        controls.setSpacing(metric.STYLE_CONTROL_GAP)
        self._fill_button = _CycleButton("fill", self._controls_row)
        self._fill_button.clicked.connect(lambda: self._cycle("fill"))
        self._dash_button = _CycleButton("dash", self._controls_row)
        self._dash_button.clicked.connect(lambda: self._cycle("dash"))
        self._strength_slider = _style_slider(tokens.STRENGTH_RANGE, self._controls_row)
        self._strength_slider.setToolTip(f"Redaction strength — {_STEP_KEYS}")
        self._strength_slider.valueChanged.connect(lambda value: self._apply(strength=value))
        self._strength_readout = _style_readout(metric.READOUT_W_STRENGTH, self._controls_row)
        self._size_slider = _style_slider(tokens.STROKE_RANGE, self._controls_row)
        self._size_slider.valueChanged.connect(lambda value: self._apply(size=value))
        self._size_readout = _style_readout(metric.READOUT_W_SIZE, self._controls_row)
        for widget in (
            self._fill_button,
            self._dash_button,
            self._strength_slider,
            self._strength_readout,
            self._size_slider,
            self._size_readout,
        ):
            controls.addWidget(widget)
        layout.addWidget(self._controls_row)

        self._section_widgets = {
            "color": (self._colour_row,),
            "fill": (self._fill_button,),
            "dash": (self._dash_button,),
            "strength": (self._strength_slider, self._strength_readout),
            "size": (self._size_slider, self._size_readout),
        }
        self.refresh()

    @property
    def tool(self) -> str | None:
        return self._tool

    def set_tool(self, tool: str | None) -> None:
        """Style `tool` from here on: its sections, holding its style."""
        self._tool = tool
        self.refresh()

    def sections(self) -> list[str]:
        """The sections showing, in the order they are laid out."""
        return [
            name for name in self.SECTIONS if not self._section_widgets[name][0].isHidden()
        ]

    def setVisible(self, visible: bool) -> None:
        # Every way a popover opens or closes -- the dot, Esc, a press on the
        # frame, the window hiding -- ends here, so the dot cannot be left
        # looking pressed over a popover that is gone.
        was_hidden = self.isHidden()
        super().setVisible(visible)
        if self.isHidden() != was_hidden:
            self.openChanged.emit(not self.isHidden())

    def refresh(self) -> None:
        """Show the tool's sections and nothing else, each holding what the
        tool's style is now.
        """
        tool = self._tool
        wanted = design.tokens.STYLE_SECTIONS.get(tool, [])
        for name, widgets in self._section_widgets.items():
            for widget in widgets:
                widget.setHidden(name not in wanted)
        self._controls_row.setHidden(not any(name in wanted for name in self.SECTIONS[1:]))

        style = self._styles.of(tool)
        for hex_colour, button in self._swatch_buttons.items():
            button.set_selected(hex_colour.lower() == style.colour.lower())
        self._fill_button.set_state(style.fill, self._cycle_tooltip("fill", style.fill))
        self._dash_button.set_state(style.dash, self._cycle_tooltip("dash", style.dash))
        for slider, value in (
            (self._strength_slider, style.strength),
            (self._size_slider, style.size),
        ):
            # Quietly: this is the popover catching up with the style, not
            # the user moving the slider.
            blocked = slider.blockSignals(True)
            slider.setValue(value)
            slider.blockSignals(blocked)
        self._strength_readout.setText(str(style.strength))
        self._size_readout.setText(f"{style.size}px")
        size_name = "Text size" if tool == "text" else "Stroke"
        self._size_slider.setToolTip(f"{size_name} — {_STEP_KEYS}")

        # Hiding a row changes the popover's height, and a layout only
        # notices on its next pass.
        self._layout.invalidate()
        self.resize(self.width(), self.sizeHint().height())

    @staticmethod
    def _cycle_tooltip(kind: str, state: str) -> str:
        if kind == "fill":
            (_name, label), (_next, next_label) = _cycle_step(design.tokens.FILL_CYCLE, state)
            return f"Fill · {label} → click for {next_label}"
        (_name, _pattern, label), (_next, _next_pattern, next_label) = _cycle_step(
            design.tokens.DASH_CYCLE, state
        )
        return f"Line · {label} → click for {next_label} — {_LINE_STYLE_KEY}"

    def _apply(self, **changes) -> None:
        if self._tool is None:
            return
        self._styles.update(self._tool, **changes)
        self.refresh()
        self.styleChanged.emit(self._tool)

    def _cycle(self, kind: str) -> None:
        cycle = design.tokens.FILL_CYCLE if kind == "fill" else design.tokens.DASH_CYCLE
        _now, then = _cycle_step(cycle, getattr(self._styles.of(self._tool), kind))
        self._apply(**{kind: then[0]})

    def _pick_custom_colour(self) -> None:
        # QColorDialog.getColor() returns an invalid QColor on Cancel rather
        # than raising or returning None, so isValid() is the "did the user
        # choose something" check.
        colour = QColorDialog.getColor(
            QColor(self._styles.of(self._tool).colour), self, "Custom Colour"
        )
        if colour.isValid():
            self._apply(colour=colour.name())

    def handle_key(self, key: int) -> bool:
        """Apply a style key to the tool, and say whether it did anything.

        1-7 pick a swatch, [ and ] step the stroke -- or a redaction's
        strength, the one slider it has -- and D cycles the line style. A
        key the tool has no section for does nothing, and is left for
        whatever else wants it.
        """
        sections = design.tokens.STYLE_SECTIONS.get(self._tool, [])
        if key in _SWATCH_KEY_CODES:
            if "color" not in sections:
                return False
            _name, hex_colour = design.tokens.INK_SWATCHES[_SWATCH_KEY_CODES[key]]
            self._apply(colour=hex_colour)
            return True
        if key in _STEP_KEY_CODES:
            step = _STEP_KEY_CODES[key]
            style = self._styles.of(self._tool)
            if "size" in sections:
                self._apply(size=style.size + step)
            elif "strength" in sections:
                self._apply(strength=style.strength + step)
            else:
                return False
            return True
        if key == _LINE_STYLE_KEY_CODE and "dash" in sections:
            self._cycle("dash")
            return True
        return False

    def reposition(self, anchor: QRect, bar: QRect, bounds: QRectF) -> None:
        """Open over `anchor` -- the style dot -- the way a family menu opens
        over its slot: above `bar`, below it when there is no room above
        inside `bounds`. All three are in the parent's coordinates.
        """
        size = QSize(self.width(), self.sizeHint().height())
        self.setGeometry(_menu_geometry(size, anchor, bar, bounds))

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        metric = design.tokens.BarMetric
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(design.bar_color("MENU_BG"))
        painter.drawRoundedRect(rect, metric.MENU_RADIUS, metric.MENU_RADIUS)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(design.bar_color("MENU_BORDER"))
        painter.drawRoundedRect(rect, metric.MENU_RADIUS, metric.MENU_RADIUS)
        painter.end()


# ---------------------------------------------------------------------------
# Capture-mode popover (SNX-44)
# ---------------------------------------------------------------------------
# docs/design/overlay-redesign.md's "Capture-mode popover" section is the
# authority here, cross-checked against the reference's own `renderVals()`
# (`menuUp = barTop > 300`, `cycleDelay`, `modes.map`) for anything the prose
# leaves implicit -- e.g. that the chip's own label follows `st.mode`, not
# just its "Region" construction default. The chip is a mode selector, not
# an action: picking a row only records `OverlayWindow._capture_mode` and
# updates the chip's label here; `_dispatch_capture_mode` is what actually
# reads it back to drive Window/Full screen picking.


class _CaptureModeRow(QPushButton):
    """One row of the popover: glyph, a two-line label/note, and a check
    mark for whichever mode is currently selected, per the spec's rows
    bullet. Hand-painted background -- like `_SwatchButton`'s ring -- since
    a hovered *and* selected row needs the hover fill to win, which a
    stylesheet's static rule can't express.
    """

    _ICON_SIZE = 16
    _CHECK_SIZE = 15
    _GAP = 10
    _LABEL_GAP = 2
    # Prose-only literals from the spec's "hover `#ffffff` at 9%" /
    # "Selected row background `#ffffff` at 8%" -- not tokens.Color entries,
    # same convention `_ToolPill._BG_ALPHA` already follows for a one-off
    # fill no other control shares.
    _HOVER_BG_ALPHA = 0.09
    _SELECTED_BG_ALPHA = 0.08

    def __init__(self, mode_label: str, icon_name: str, note: str, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFlat(True)
        self.setStyleSheet("QPushButton { border: none; background: transparent; }")

        self._icon_name = icon_name
        self._selected = False
        self._bg_alpha: float | None = None

        metric = design.tokens.Metric
        layout = QHBoxLayout(self)
        layout.setContentsMargins(
            metric.MENU_ROW_PAD_H,
            metric.MENU_ROW_PAD_V,
            metric.MENU_ROW_PAD_H,
            metric.MENU_ROW_PAD_V,
        )
        layout.setSpacing(self._GAP)

        self._icon_label = QLabel(self)
        self._icon_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        layout.addWidget(self._icon_label)

        text_column = QVBoxLayout()
        text_column.setContentsMargins(0, 0, 0, 0)
        text_column.setSpacing(self._LABEL_GAP)

        self._label = QLabel(mode_label, self)
        self._label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        label_font = QFont(design.font_families().ui)
        size, weight = design.tokens.Font.MENU_LABEL
        label_font.setPixelSize(round(size))
        label_font.setWeight(QFont.Weight(weight))
        self._label.setFont(label_font)
        text_column.addWidget(self._label)

        note_label = QLabel(note, self)
        note_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        note_font = QFont(design.font_families().ui)
        size, weight = design.tokens.Font.MENU_NOTE
        note_font.setPixelSize(round(size))
        note_font.setWeight(QFont.Weight(weight))
        note_label.setFont(note_font)
        note_label.setStyleSheet(f"color: {design.color('TEXT_MUTED').name()};")
        text_column.addWidget(note_label)

        layout.addLayout(text_column, 1)

        self._check = QLabel(self)
        self._check.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._check.setPixmap(
            design.icon("check", design.color("ACCENT")).pixmap(
                self._CHECK_SIZE, self._CHECK_SIZE
            )
        )
        self._check.setVisible(False)
        layout.addWidget(self._check)

        self._refresh()

    @property
    def is_selected(self) -> bool:
        return self._selected

    def set_selected(self, selected: bool) -> None:
        """Mark this row as the current capture mode -- the check glyph and
        the `ICON_ACTIVE` label/icon colour both follow `_selected`, per
        the spec's "a check glyph... for the selected row."
        """
        self._selected = selected
        self._refresh()

    def _refresh(self, hovered: bool | None = None) -> None:
        if hovered is None:
            hovered = self.underMouse()
        # Hover wins over the selected fill when both apply -- every row,
        # including the current mode's own, still needs to read as
        # clickable while the pointer is over it.
        if hovered:
            self._bg_alpha = self._HOVER_BG_ALPHA
        elif self._selected:
            self._bg_alpha = self._SELECTED_BG_ALPHA
        else:
            self._bg_alpha = None

        fg = design.color("ICON_ACTIVE") if self._selected else design.color("ICON_IDLE")
        self._label.setStyleSheet(f"color: {fg.name()};")
        self._icon_label.setPixmap(
            design.icon(self._icon_name, fg).pixmap(self._ICON_SIZE, self._ICON_SIZE)
        )
        self._check.setVisible(self._selected)
        self.update()

    def enterEvent(self, event) -> None:
        self._refresh(hovered=True)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._refresh(hovered=False)
        super().leaveEvent(event)

    def sizeHint(self) -> QSize:
        # QPushButton.sizeHint() sizes off `self.text()`/`self.icon()` --
        # both unused here, since the glyph/label/note/check pairing lives
        # in the child QHBoxLayout instead (see class docstring), so the
        # base implementation falls back to a near-empty placeholder height
        # regardless of the two-line label actually painted. That's what
        # SNX-75 found: a row measured 48x12 against the ~45px a 12.5px
        # name over an 11px note plus 8px top/bottom padding actually needs,
        # so the popover's QVBoxLayout gave it almost no height and the
        # whole menu collapsed -- the same class of bug SNX-59 fixed for
        # `_PillButton` (see its own sizeHint docstring). The child layout
        # already knows the true height, because it was built from
        # MENU_ROW_PAD_V plus the icon/text column's own sizeHint, and the
        # label/note sizeHints come from the fonts they actually render in.
        return self.layout().sizeHint()

    def minimumSizeHint(self) -> QSize:
        return self.sizeHint()

    def paintEvent(self, event) -> None:
        if self._bg_alpha is None:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        bg = QColor("#ffffff")
        bg.setAlphaF(self._bg_alpha)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(bg)
        metric = design.tokens.Metric
        painter.drawRoundedRect(
            QRectF(self.rect()), metric.MENU_ROW_RADIUS, metric.MENU_ROW_RADIUS
        )
        painter.end()


class _DelayRow(QPushButton):
    """The popover's delay row: a timer glyph, the word "Delay", and the
    current value right-aligned -- per the spec's "Delay" paragraph. No
    selected state (unlike `_CaptureModeRow`): the row itself *is* the
    control, and there is nothing else in the popover it could read as
    selected relative to.
    """

    _ICON_SIZE = 16
    _GAP = 10
    _HOVER_BG_ALPHA = 0.09  # same one-off fill _CaptureModeRow's hover uses
    # "mono 11.5px `#8f9689`" -- the closest tokens.Font entry, MENU_NOTE,
    # is 11.0px and already spoken for by the mode rows' own notes, so this
    # stays a local literal.
    _VALUE_PX = 11.5
    _VALUE_WEIGHT = 400

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFlat(True)
        self.setStyleSheet("QPushButton { border: none; background: transparent; }")

        self._hovered = False

        metric = design.tokens.Metric
        layout = QHBoxLayout(self)
        layout.setContentsMargins(
            metric.MENU_ROW_PAD_H,
            metric.MENU_ROW_PAD_V,
            metric.MENU_ROW_PAD_H,
            metric.MENU_ROW_PAD_V,
        )
        layout.setSpacing(self._GAP)

        icon_label = QLabel(self)
        icon_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        icon_label.setPixmap(
            design.icon("timer", design.color("ICON_IDLE")).pixmap(
                self._ICON_SIZE, self._ICON_SIZE
            )
        )
        layout.addWidget(icon_label)

        label = QLabel("Delay", self)
        label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        label_font = QFont(design.font_families().ui)
        size, weight = design.tokens.Font.MENU_LABEL
        label_font.setPixelSize(round(size))
        label_font.setWeight(QFont.Weight(weight))
        label.setFont(label_font)
        label.setStyleSheet(f"color: {design.color('ICON_IDLE').name()};")
        layout.addWidget(label, 1)

        self._value = QLabel(self)
        self._value.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        value_font = QFont(design.font_families().mono)
        value_font.setPixelSize(round(self._VALUE_PX))
        value_font.setWeight(QFont.Weight(self._VALUE_WEIGHT))
        self._value.setFont(value_font)
        self._value.setStyleSheet(f"color: {design.color('TEXT_MUTED').name()};")
        layout.addWidget(self._value)

    def set_value(self, text: str) -> None:
        self._value.setText(text)

    def enterEvent(self, event) -> None:
        self._hovered = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._hovered = False
        self.update()
        super().leaveEvent(event)

    def sizeHint(self) -> QSize:
        # Same fix as `_CaptureModeRow.sizeHint` and for the same reason:
        # this is a QPushButton whose real content lives in a child layout,
        # so the base sizeHint() -- keyed off the unused text()/icon() --
        # under-reports it. The popover's own height is the sum of its
        # rows' sizeHints (AC), so a delay row that still collapsed would
        # undersize the popover even with every mode row already fixed.
        return self.layout().sizeHint()

    def minimumSizeHint(self) -> QSize:
        return self.sizeHint()

    def paintEvent(self, event) -> None:
        if not self._hovered:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        bg = QColor("#ffffff")
        bg.setAlphaF(self._HOVER_BG_ALPHA)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(bg)
        metric = design.tokens.Metric
        painter.drawRoundedRect(
            QRectF(self.rect()), metric.MENU_ROW_RADIUS, metric.MENU_ROW_RADIUS
        )
        painter.end()


class _MenuSeparator(QWidget):
    """The 1px rule between the mode rows and the delay row, per the
    spec's "a 1px `#ffffff` 10% separator with 5px/4px margins." A fixed-
    height widget that paints its line inset from its own edges, mirroring
    how `_Divider` fixes its own size rather than leaning on layout
    margins for a hairline.
    """

    _MARGIN_V = 5
    _MARGIN_H = 4

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(self._MARGIN_V * 2 + 1)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        # BAR_BORDER is the same #ffffff/10% pair the spec's separator
        # uses -- reused rather than re-typed, the same precedent
        # FloatingBar._build_save_button already sets for BAR_BORDER.
        painter.fillRect(
            self._MARGIN_H,
            self._MARGIN_V,
            self.width() - 2 * self._MARGIN_H,
            1,
            design.color("BAR_BORDER"),
        )
        painter.end()


class CaptureModePopover(_Chrome):
    """The overlay redesign's capture-mode popover: `tokens.CAPTURE_MODES`
    as a list of rows, a separator, then the delay row -- per
    docs/design/overlay-redesign.md's "Capture-mode popover" section.

    A real child widget of `OverlayWindow`, built the same way
    `FloatingBar`/`StylePopover` are -- opened and positioned by
    `OverlayWindow._toggle_capture_popover`, never painted in its own
    `paintEvent`. Picking a row only records the choice and closes the
    popover; Window and Full screen don't do anything past that
    yet -- they're separate tickets in the same arc `_bar`'s tool buttons
    already follow (see `OverlayWindow._on_tool_selected`'s docstring).
    """

    modeSelected = pyqtSignal(str)
    delayChanged = pyqtSignal(str)

    # The README gives this literal directly ("if bar top > 300px") rather
    # than as a tokens.Metric entry -- same convention FloatingBar's own
    # _TOP_MAX_FROM_BOTTOM already follows for a prose-only constant.
    _UP_THRESHOLD = 300

    # Same #1a1c18 BAR_BG already names, at the popover's own 97% rather
    # than the bar's 93% -- no tokens.Color entry carries that exact alpha,
    # so it's a local literal rather than a one-off *_ALPHA sibling added
    # for a single caller.
    _BG_ALPHA = 0.97

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

        metric = design.tokens.Metric
        # Fixed, not just flowed from its rows' natural width -- the spec
        # gives the popover an exact "262px wide," and `reposition` below
        # positions off that same literal rather than a sizeHint that could
        # drift from what actually gets painted.
        self.setFixedWidth(metric.MENU_W)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(
            metric.MENU_PAD, metric.MENU_PAD, metric.MENU_PAD, metric.MENU_PAD
        )
        layout.setSpacing(0)

        self._mode: str = design.tokens.CAPTURE_MODES[0][0]
        self._delay: str = design.tokens.DELAYS[0]

        self._rows: dict[str, _CaptureModeRow] = {}
        for label, icon_name, note in design.tokens.CAPTURE_MODES:
            row = _CaptureModeRow(label, icon_name, note, self)
            row.clicked.connect(lambda checked=False, m=label: self._on_row_clicked(m))
            self._rows[label] = row
            layout.addWidget(row)

        layout.addWidget(_MenuSeparator(self))

        self._delay_row = _DelayRow(self)
        self._delay_row.clicked.connect(self._on_delay_clicked)
        layout.addWidget(self._delay_row)

        self._select_row(self._mode)
        self._delay_row.set_value(self._delay)

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def delay(self) -> str:
        return self._delay

    def set_mode(self, mode: str) -> None:
        """Mark `mode`'s row checked without emitting `modeSelected` or
        closing the popover -- for a future caller seeding the popover from
        elsewhere, mirroring the split `FloatingBar.set_active_tool` keeps
        from its own `_on_tool_clicked`.
        """
        self._mode = mode
        self._select_row(mode)

    def set_delay(self, delay: str) -> None:
        """Show `delay` without emitting `delayChanged` -- the chooser row
        sets the same delay, and this popover has to agree with it (#73).
        """
        if delay not in design.tokens.DELAYS:
            return
        self._delay = delay
        self._delay_row.set_value(delay)

    def _select_row(self, mode: str) -> None:
        for label, row in self._rows.items():
            row.set_selected(label == mode)

    def _on_row_clicked(self, mode: str) -> None:
        """Record `mode` and close the popover, per the spec's "picking a
        row records that mode and closes the popover." Modes past Region
        are separate tickets -- this never itself starts a window hover-
        highlight, only the recording.
        """
        self.set_mode(mode)
        self.modeSelected.emit(mode)
        self.hide()

    def _on_delay_clicked(self) -> None:
        """Cycle to the next `tokens.DELAYS` value, wrapping past the last
        back to the first -- per the spec's "Clicking cycles Off -> 3s ->
        5s -> 10s -> Off." Unlike a mode row, this leaves the popover open,
        mirroring the reference's own `cycleDelay`, which never touches
        `modeOpen`.
        """
        delays = design.tokens.DELAYS
        index = delays.index(self._delay)
        self._delay = delays[(index + 1) % len(delays)]
        self._delay_row.set_value(self._delay)
        self.delayChanged.emit(self._delay)

    def reposition(self, bar_geometry: QRect, bounds: QRectF) -> None:
        """Position the popover against `bar_geometry` (`FloatingBar`'s own
        geometry, already in this widget's parent's coordinate space), per
        the spec's rule: "if bar top > 300px, place the popover at
        bar_top - popover_height - 8; otherwise place it below the bar."
        Horizontally centred on the bar and clamped inside `bounds`,
        mirroring `OverlayWindow._reposition_tool_hint`'s own centring.

        `bounds` is the selection's own monitor, in parent coordinates --
        see `FloatingBar.reposition`, which this deliberately mirrors. The
        spec's "300px" is a distance from the top of the *screen* the user
        is looking at, so it is measured from `bounds.top()` rather than
        read as an absolute parent coordinate: a monitor mounted 201px down
        the virtual desktop would otherwise flip the popover upward 201px
        too early, into a gap no monitor displays.
        """
        metric = design.tokens.Metric
        width = metric.MENU_W
        height = self.sizeHint().height()

        center_x = bar_geometry.center().x()
        left = center_x - width / 2
        left = max(bounds.left(), min(left, bounds.right() - width))

        if bar_geometry.top() - bounds.top() > self._UP_THRESHOLD:
            top = bar_geometry.top() - height - metric.MENU_OFFSET
        else:
            top = bar_geometry.bottom() + metric.MENU_OFFSET
        top = max(bounds.top(), min(top, bounds.bottom() - height))

        self.setGeometry(round(left), round(top), width, height)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        metric = design.tokens.Metric
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)

        bg = QColor(design.tokens.Color.BAR_BG)
        bg.setAlphaF(self._BG_ALPHA)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(bg)
        painter.drawRoundedRect(rect, metric.MENU_RADIUS, metric.MENU_RADIUS)

        # DIVIDER is the same #ffffff/12% pair the spec's popover border
        # uses -- reused rather than re-typed, the same precedent
        # FloatingBar._build_save_button sets for BAR_BORDER.
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(design.color("DIVIDER"))
        painter.drawRoundedRect(rect, metric.MENU_RADIUS, metric.MENU_RADIUS)
        painter.end()


# ---------------------------------------------------------------------------
# Family menus
# ---------------------------------------------------------------------------
# A family slot's notch opens one of these: every sibling in the family, the
# one the slot shows ticked. Shapes carry each sibling's key, since the menu
# is for discovering a shape rather than for reaching it; redaction rows
# carry what each one guarantees instead, because blur on small text is
# famously recoverable and a user choosing between the three needs to know.


def _menu_geometry(size: QSize, anchor: QRect, bar: QRect, bounds: QRectF) -> QRect:
    """Where a menu of `size` opens for the control at `anchor` on `bar`:
    centred over it and `MENU_OFFSET` above the bar, or below the bar when
    there is no room above inside `bounds`. All in the parent's coordinates.

    Shared by the family menus and the style popover, so every menu off the
    bar opens the same way.
    """
    metric = design.tokens.BarMetric
    width, height = size.width(), size.height()
    left = anchor.center().x() - width / 2
    left = max(bounds.left(), min(left, bounds.right() - width))
    bar_rect = QRectF(bar)
    top = bar_rect.top() - metric.MENU_OFFSET - height
    if top < bounds.top():
        top = min(bar_rect.bottom() + metric.MENU_OFFSET, bounds.bottom() - height)
    return QRect(round(left), round(top), width, height)


class _FamilyRow(QPushButton):
    """One sibling in a family menu: glyph, label, then its key or its note,
    and a tick for the sibling the slot is showing.

    Painted rather than laid out from child labels, and sized from the fonts
    it actually draws in -- so it cannot collapse to `QPushButton`'s
    placeholder height the way `_CaptureModeRow.sizeHint` documents.
    """

    def __init__(
        self,
        tool: str,
        glyph: str,
        label: str,
        shortcut: str,
        note: str,
        width: int,
        parent=None,
    ):
        super().__init__(parent)
        self.tool = tool
        self._glyph = glyph
        self._label = label
        self._shortcut = shortcut
        self._note = note
        self._selected = False
        self._hovered = False
        self.setFlat(True)
        self.setStyleSheet("QPushButton { border: none; background: transparent; }")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(width, self._height())

    def sizeHint(self) -> QSize:
        # The size it is fixed at. `QPushButton`'s own hint measures a text
        # and an icon this row never sets.
        return QSize(self.minimumWidth(), self.minimumHeight())

    def minimumSizeHint(self) -> QSize:
        return self.sizeHint()

    @staticmethod
    def _font(spec: tuple[float, int], mono: bool = False) -> QFont:
        families = design.font_families()
        font = QFont(families.mono if mono else families.ui)
        size, weight = spec
        font.setPixelSize(round(size))
        font.setWeight(QFont.Weight(weight))
        return font

    def _height(self) -> int:
        metric = design.tokens.BarMetric
        pad_v, _pad_h = metric.MENU_ROW_PAD
        content = QFontMetricsF(self._font(design.tokens.BarFont.MENU_LABEL)).height()
        if self._note:
            content += metric.MENU_NOTE_GAP + QFontMetricsF(
                self._font(design.tokens.BarFont.MENU_NOTE)
            ).height()
        return math.ceil(pad_v * 2 + max(metric.MENU_ROW_ICON, content))

    @property
    def is_selected(self) -> bool:
        return self._selected

    def set_selected(self, selected: bool) -> None:
        self._selected = selected
        self.update()

    def enterEvent(self, event) -> None:
        self._hovered = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._hovered = False
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        metric = design.tokens.BarMetric
        fonts = design.tokens.BarFont
        rect = QRectF(self.rect())
        pad_v, pad_h = metric.MENU_ROW_PAD

        # Hover wins over the selected fill, so the ticked row still reads as
        # something that can be pressed.
        if self._hovered or self._selected:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(
                design.bar_color("ROW_HOVER_BG" if self._hovered else "ROW_SELECTED_BG")
            )
            painter.drawRoundedRect(rect, metric.MENU_ROW_RADIUS, metric.MENU_ROW_RADIUS)

        fg = design.bar_color("ROW_SELECTED_FG" if self._selected else "ROW_IDLE_FG")
        icon = metric.MENU_ROW_ICON
        left = float(pad_h)
        design.icon(self._glyph, fg).paint(
            painter, round(left), round((rect.height() - icon) / 2), icon, icon
        )
        left += icon + metric.MENU_ROW_GAP

        tick = metric.MENU_TICK
        right = rect.width() - pad_h - tick
        if self._selected:
            design.icon("check", design.bar_color("ACCENT")).paint(
                painter, round(right), round((rect.height() - tick) / 2), tick, tick
            )
        right -= metric.MENU_ROW_GAP

        if self._shortcut:
            font = self._font(fonts.MENU_SHORTCUT, mono=True)
            painter.setFont(font)
            painter.setPen(design.bar_color("SHORTCUT_FG"))
            width = QFontMetricsF(font).horizontalAdvance(self._shortcut)
            painter.drawText(
                QRectF(right - width, 0, width, rect.height()),
                int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
                self._shortcut,
            )
            right -= width + metric.MENU_ROW_GAP

        label_font = self._font(fonts.MENU_LABEL)
        painter.setFont(label_font)
        painter.setPen(fg)
        if not self._note:
            painter.drawText(
                QRectF(left, 0, right - left, rect.height()),
                int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                self._label,
            )
            painter.end()
            return

        label_h = QFontMetricsF(label_font).height()
        painter.drawText(
            QRectF(left, pad_v, right - left, label_h),
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            self._label,
        )
        note_font = self._font(fonts.MENU_NOTE)
        painter.setFont(note_font)
        painter.setPen(design.bar_color("ROW_NOTE_FG"))
        painter.drawText(
            QRectF(
                left,
                pad_v + label_h + metric.MENU_NOTE_GAP,
                right - left,
                QFontMetricsF(note_font).height(),
            ),
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            self._note,
        )
        painter.end()


class FamilyMenu(_Chrome):
    """A family slot's menu: every sibling, the one the slot shows ticked,
    and a pick that arms the sibling picked.

    A child of the window the bar sits over -- not of the bar, and not a
    top-level popup either, though the handoff draws one. Not the bar's,
    because a menu paints outside the bar's rect and above the strip that
    hangs under it. Not a popup, because a popup takes the keyboard while it
    is open, so a sibling's key and Esc would stop reaching the window, and
    a press outside a popup is replayed underneath or swallowed depending on
    the platform. As a child, the window's own press handler decides what a
    press outside the menu means -- close it, and nothing else -- and
    `_Chrome` keeps a press on the menu from ever reaching that handler.
    """

    siblingPicked = pyqtSignal(str)

    def __init__(self, family: str, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.family = family
        metric = design.tokens.BarMetric
        tokens = design.tokens
        if family == "shapes":
            width = metric.MENU_W_SHAPES
            entries = [(tool, tool, label, key, "") for tool, label, key in tokens.SHAPES]
        else:
            width = metric.MENU_W_REDACT
            entries = [
                (tool, glyph, label, "", note) for tool, glyph, label, note in tokens.REDACTIONS
            ]
        # Border-box, as the handoff insists: the width is the menu's whole
        # outside edge, padding and border included.
        self.setFixedWidth(width)
        inset = metric.MENU_PAD + metric.BORDER
        layout = QVBoxLayout(self)
        layout.setContentsMargins(inset, inset, inset, inset)
        layout.setSpacing(0)

        self._rows: dict[str, _FamilyRow] = {}
        for tool, glyph, label, key, note in entries:
            row = _FamilyRow(tool, glyph, label, key, note, width - 2 * inset, self)
            row.clicked.connect(lambda checked=False, t=tool: self._on_row_clicked(t))
            self._rows[tool] = row
            layout.addWidget(row)

        self._current = entries[0][0]
        self.set_current(self._current)
        self.resize(self.sizeHint())

    @property
    def current(self) -> str:
        return self._current

    def set_current(self, tool: str) -> None:
        """Tick `tool` without arming anything -- for seeding the menu with
        the sibling its slot shows before it opens.
        """
        self._current = tool
        for name, row in self._rows.items():
            row.set_selected(name == tool)

    def _on_row_clicked(self, tool: str) -> None:
        self.set_current(tool)
        self.hide()
        self.siblingPicked.emit(tool)

    def reposition(self, slot: QRect, bar: QRect, bounds: QRectF) -> None:
        """Centre over `slot` and open above `bar`, `MENU_OFFSET` clear of
        it -- below the bar instead when there is no room above inside
        `bounds`. All three are in the parent's coordinates.

        Above first, as the spec opens every menu: the bar sits below the
        selection whenever it can, so a menu opening downward would hang off
        the bottom of the monitor more often than not.
        """
        size = QSize(self.width(), self.sizeHint().height())
        self.setGeometry(_menu_geometry(size, slot, bar, bounds))

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        metric = design.tokens.BarMetric
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(design.bar_color("MENU_BG"))
        painter.drawRoundedRect(rect, metric.MENU_RADIUS, metric.MENU_RADIUS)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(design.bar_color("MENU_BORDER"))
        painter.drawRoundedRect(rect, metric.MENU_RADIUS, metric.MENU_RADIUS)
        painter.end()


# ---------------------------------------------------------------------------
# Top hint HUD (SNX-46)
# ---------------------------------------------------------------------------
# docs/design/overlay-redesign.md's "Top hint HUD" section is the authority
# here. It's a first-run affordance, behind a preference (`hints`) --
# see `OverlayWindow.set_hints_enabled` -- and the spec's own "Re-framing"
# section already reserves the room for it: `OverlayWindow._TOP_CLEARANCE`
# (52) is 8px clear of `tokens.Metric.HUD_H` (44), so the two have agreed
# since SNX-33 landed and this ticket only has to paint into the space
# already held open.
#
# SNX-65: the preference now defaults *off*. Read as a banner across the
# whole top of every capture rather than help, it was the first thing the
# eye landed on and competed with the snip it sits above -- exactly the
# "first-run affordance worth hiding after N successful captures" this
# section already called it out as. `_resize_selection` stops reserving
# `_TOP_CLEARANCE` the moment hints are off, so the selection gets the room
# back rather than keeping a dead strip held open for a bar nobody is
# shown; press `?` to bring it up for the session (`keyPressEvent` below).


class HintHUD(_Chrome):
    """The overlay's full-width top hint bar, per docs/design/overlay-
    redesign.md's "Top hint HUD" section: `Esc discard ink · Enter copy &
    dismiss · P H R O L A S T B E pick a tool · drag any edge to re-frame -- the
    ink stays where you put it`.

    A real child widget of `OverlayWindow` -- built from real `QLabel`
    segments, not painted inside `OverlayWindow.paintEvent` -- the same
    reasoning `Toast` documents for itself: `OverlayWindow.rendered_image`
    flattens `_marks` onto the frame via `shapes.render_selection` and never
    touches this window's chrome at all, so the HUD can't leak into an
    export regardless of whether it happens to be on screen at the moment
    `copy`/`save` is called.

    Key names (`Esc`, `Enter`, the eight tool shortcuts) are their own
    labels in the mono family at `HUD_KEY` (pure white); the surrounding
    prose is the UI family at the muted `HUD_TEXT` -- per the spec's "Key
    names are mono in pure white," so a key reads as a key at a glance
    rather than blending into the sentence around it.
    """

    # (text, is_key) in reading order. The key segment for the tool
    # shortcuts is built from the bar's own slots (`_bar_keys`) rather than
    # typed out -- if a shortcut or the slot order ever changes, this line
    # follows it instead of silently drifting out of sync the way a
    # hand-typed copy of the same letters could.
    def _segments(self) -> list[tuple[str, bool]]:
        keys = " ".join(_bar_keys())
        return [
            ("Esc", True),
            (" discard ink · ", False),
            ("Enter", True),
            (" copy & dismiss · ", False),
            (keys, True),
            (
                " pick a tool · drag any edge to re-frame — the ink"
                " stays where you put it",
                False,
            ),
        ]

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setFixedHeight(design.tokens.Metric.HUD_H)

        # Full-width and centred, per "Full width... contents centred" --
        # a stretch on both sides of the segment labels is what centres a
        # variable-width run of text without this widget needing to measure
        # it itself the way OverlayWindow's own chips do.
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addStretch(1)
        for text, is_key in self._segments():
            layout.addWidget(self._segment_label(text, is_key))
        layout.addStretch(1)

    def _segment_label(self, text: str, is_key: bool) -> QLabel:
        label = QLabel(text, self)
        label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        family = design.font_families().mono if is_key else design.font_families().ui
        font = QFont(family)
        size, weight = design.tokens.Font.HUD
        font.setPixelSize(round(size))
        font.setWeight(QFont.Weight(weight))
        label.setFont(font)
        colour = design.color("HUD_KEY" if is_key else "HUD_TEXT")
        label.setStyleSheet(f"color: {colour.name()};")
        return label

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        # A flat, translucent fill -- per the Qt notes' "cheaper fallback:
        # raise the fill alpha... and skip the blur," the same trade-off
        # FloatingBar/StylePopover/CaptureModePopover already make for their
        # own backdrop-filter blur.
        painter.fillRect(QRectF(self.rect()), design.color("HUD_BG"))
        painter.end()


# ---------------------------------------------------------------------------
# Toast (SNX-45)
# ---------------------------------------------------------------------------


class Toast(_Chrome):
    """The overlay redesign's toast: bottom centre, above everything, per
    docs/design/overlay-redesign.md's "Toast" section.

    A real child widget of `OverlayWindow`, built and positioned the same
    way `FloatingBar`/`StylePopover`/`CaptureModePopover` are -- never
    painted inside `OverlayWindow.paintEvent`, which is what the spec means
    by "chrome painted over the overlay rather than something drawn into
    the frame": `OverlayWindow.rendered_image` flattens `_marks` onto the
    frame's own pixmap via `shapes.render_selection` and never touches this
    widget (or any other chrome) at all, so a toast can never end up in an
    export regardless of whether it happens to be on screen at the moment
    `copy`/`save` is called.

    There is only ever one toast, per the spec's "a new toast replaces the
    old one and restarts the timer" -- `show_message` overwrites whatever
    the previous call was showing and restarts `_timer` rather than a
    caller stacking a second widget, since this class is itself the single
    instance `OverlayWindow` keeps as `_toast`.
    """

    _ICON_SIZE = 15
    # Prose-only spacing between the glyph and the message, like
    # `_ToolPill`/`_FROZEN_INNER_GAP`'s own un-tokenized gaps elsewhere in
    # this file.
    _GAP = 8

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        metric = design.tokens.Metric

        layout = QHBoxLayout(self)
        layout.setContentsMargins(
            metric.TOAST_PAD_H, metric.TOAST_PAD_V, metric.TOAST_PAD_H, metric.TOAST_PAD_V
        )
        layout.setSpacing(self._GAP)

        self._icon_label = QLabel(self)
        self._icon_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        layout.addWidget(self._icon_label)

        self._text_label = QLabel(self)
        self._text_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        font = QFont(design.font_families().ui)
        size, weight = design.tokens.Font.TOAST
        font.setPixelSize(round(size))
        font.setWeight(QFont.Weight(weight))
        self._text_label.setFont(font)
        self._text_label.setStyleSheet(f"color: {design.color('TOAST_FG').name()};")
        layout.addWidget(self._text_label)

        # Single-shot, restarted (not re-created) by every show_message
        # call -- QTimer.start() on an already-running timer resets its
        # remaining time, which is exactly the spec's "restarts the timer"
        # rather than letting an earlier call's dismissal fire early.
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(metric.TOAST_MS)
        self._timer.timeout.connect(self.hide)

        self.hide()

    def show_message(self, icon_name: str, text: str, bounds: QRectF) -> None:
        """Show `text` next to `icon_name`'s glyph, positioned at the
        bottom centre of `bounds`, and (re)start the `TOAST_MS`
        auto-dismiss timer.

        Updates this same widget's content rather than creating a new one
        -- there is only ever one toast on screen, per the class docstring
        -- so a second call while the first is still showing both replaces
        the message and restarts the timer in one step.

        `bounds` is the selection's own monitor rather than the parent's
        size, for the reason `FloatingBar.reposition` sets out at length:
        "the bottom centre of the window" is the bottom centre of the whole
        virtual desktop once one window spans every monitor, which is both
        the wrong monitor to confirm a snip on and, where monitor heights
        differ, potentially a gap that displays nothing at all.
        """
        metric = design.tokens.Metric
        pixmap = design.icon(icon_name, design.color("TOAST_FG")).pixmap(
            self._ICON_SIZE, self._ICON_SIZE
        )
        self._icon_label.setPixmap(pixmap)
        self._text_label.setText(text)

        size = self.sizeHint()
        left = bounds.left() + (bounds.width() - size.width()) / 2
        top = bounds.bottom() - metric.TOAST_BOTTOM - size.height()
        self.setGeometry(round(left), round(top), size.width(), size.height())

        self.show()
        self.raise_()  # "above everything" -- including the bar and trays
        self._timer.start()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        metric = design.tokens.Metric

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(design.color("TOAST_BG"))
        painter.drawRoundedRect(QRectF(self.rect()), metric.TOAST_RADIUS, metric.TOAST_RADIUS)
        painter.end()


# ---------------------------------------------------------------------------
# Close button (SNX-80)
# ---------------------------------------------------------------------------
# `_bar` and the per-selection chips (the dimension chip, the frozen pill)
# all only ever exist once a selection does -- `_sync_bar_visibility`,
# `OverlayWindow.paintEvent`'s own `if self._selection is not None` guard --
# but a user who opens the overlay and decides, before dragging anything at
# all, that they don't want to snip anything has exactly as much right to a
# visible way out as one who's already mid-annotation. SNX-65 turned off the
# only thing (the hint HUD) that ever told anyone Escape was that way out,
# so this is its own small piece of chrome, independent of both: shown for
# as long as `OverlayWindow` itself is (`showEvent`/`hideEvent`, the same
# pairing `_toast`/`_hud` already use), never gated on `_selection`,
# `_bar.active_tool` or `_marks` the way everything else in this file is.


class _CloseButton(QPushButton):
    """A small round button pinned to a fixed corner of the overlay: the
    one control guaranteed to be on screen, and clickable, from the moment
    the overlay opens to the moment it closes.

    Unlike `_IconButton` (idle-transparent, relying on the bar's own glass
    fill for contrast against the frozen desktop underneath), this button
    has no bar behind it -- it sits directly over whatever pixels happen to
    be there, which could be any colour at all -- so its background is
    always painted, not just on hover, using the same `BAR_BG`/
    `BAR_BG_ALPHA` glass the floating bar itself uses. A plain QSS
    `:hover` rule is enough for the hover state (no custom paintEvent
    needed, unlike `_SwatchButton`'s two-colour ring): there is only ever
    one fill to swap.
    """

    _SIZE = design.tokens.Metric.BTN
    _ICON_SIZE = design.tokens.Metric.ICON
    # How much brighter the fill reads on hover -- enough to register as a
    # state change without a dedicated token for a control this small, the
    # same un-tokenized-literal convention `OverlayWindow`'s own
    # `_CORNER_BRACKET_OFFSET` and friends already follow.
    _HOVER_ALPHA_BOOST = 0.07

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(self._SIZE, self._SIZE)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        # Names Escape by its full word, not the "Esc" abbreviation every
        # other tooltip in this file uses for a shortcut -- this is the
        # one place a user with the hint HUD off (SNX-65's new default)
        # learns the keyboard route exists at all, so it spells the key
        # out rather than assuming the abbreviation is already familiar.
        self.setToolTip("Close — Escape")
        self.setFlat(True)

        bg = design.color("BAR_BG")
        hover_bg = QColor(bg)
        hover_bg.setAlphaF(min(1.0, bg.alphaF() + self._HOVER_ALPHA_BOOST))
        self.setStyleSheet(
            "QPushButton { border: none; border-radius: %dpx;"
            " background: rgba(%d, %d, %d, %s); }"
            "QPushButton:hover { background: rgba(%d, %d, %d, %s); }"
            % (
                self._SIZE // 2,
                bg.red(), bg.green(), bg.blue(), bg.alphaF(),
                hover_bg.red(), hover_bg.green(), hover_bg.blue(), hover_bg.alphaF(),
            )
        )
        self.setIcon(design.icon("close", design.color("TEXT_PRIMARY")))
        self.setIconSize(QSize(self._ICON_SIZE, self._ICON_SIZE))


# ---------------------------------------------------------------------------
# Delay countdown (SNX-50)
# ---------------------------------------------------------------------------
# docs/design/overlay-redesign.md's "Capture modes" entry for Delay is the
# authority: "the overlay dismisses, waits, re-grabs and re-opens. Show a
# countdown." The prototype never simulates Delay (Region is the only mode it
# simulates, per that same section's opening line), so there is no reference
# markup for the countdown's own look -- styled here from the same BAR_BG
# glass and TEXT_PRIMARY/mono readout the rest of this file's chrome already
# uses, rather than inventing a one-off palette for a single widget.


class DelayCountdown(QWidget):
    """A small, top-level countdown shown while `OverlayWindow` is hidden
    for a delayed re-capture (`OverlayWindow._start_delayed_capture`).

    Deliberately *not* a child of `OverlayWindow`, unlike every other piece
    of chrome in this file: the entire reason the overlay hides for the
    delay is so it isn't in its own screenshot (per the spec: "the overlay
    dismisses... so it is not in its own screenshot" is the point of Delay
    in the first place), and a child widget goes invisible the instant its
    parent does -- see `OverlayWindow.hideEvent`, which relies on exactly
    that to take `_bar`/`_style_popover`/`_popover`/`_toast`/`_hud` down with it. A
    countdown built the same way would vanish along with the window it is
    supposed to be standing in for, which is the one thing it must not do.
    """

    _SIZE = 96
    _BG_ALPHA = 0.72  # the same "glass over the desktop" treatment as the bar

    def __init__(self):
        # No parent, ever -- see the class docstring.
        super().__init__(None)
        # Frameless/always-on-top so it reads as a HUD rather than a window
        # a user could accidentally click into and lose focus of, mirroring
        # `Overlay`/`OverlayWindow`'s own `setWindowFlags` calls.
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setFixedSize(self._SIZE, self._SIZE)

        self._label = QLabel(self)
        self._label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._label.setGeometry(0, 0, self._SIZE, self._SIZE)
        font = QFont(design.font_families().mono)
        font.setPixelSize(36)
        font.setWeight(QFont.Weight(600))
        self._label.setFont(font)
        self._label.setStyleSheet(f"color: {design.color('TEXT_PRIMARY').name()};")

    def set_seconds_remaining(self, seconds: int) -> None:
        self._label.setText(str(seconds))

    def show_centered_on(self, geometry: QRect) -> None:
        """Position centred over `geometry` -- the monitor the hidden
        `OverlayWindow` was being worked on, in absolute coordinates -- and
        show.
        """
        center = QRectF(geometry).center()
        self.move(round(center.x() - self._SIZE / 2), round(center.y() - self._SIZE / 2))
        self.show()
        self.raise_()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        bg = QColor(design.tokens.Color.BAR_BG)
        bg.setAlphaF(self._BG_ALPHA)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(bg)
        painter.drawEllipse(QRectF(self.rect()))
        painter.end()


@dataclass(frozen=True)
class _MarkAction:
    """One entry in `OverlayWindow`'s undo/redo history (SNX-39; SNX-70
    folds the eraser into it, SNX-72 folds `clear` in too): `kind` is
    `'add'` for a mark `add_mark` appended, `'erase'` for one `erase_at`
    removed, or `'clear'` for the whole mark list `clear()` emptied in one
    step. `index` is its position in `_marks`' draw order at the moment
    that action ran -- unused for `'clear'`, since it always empties from
    and restores to the full list rather than one position in it. `shape`
    holds the single removed/added `Shape` for `'add'`/`'erase'`, or the
    tuple of every mark that was on screen for `'clear'`. `undo`/`redo`
    invert/replay the trio -- see their own docstrings -- which is what
    lets an erase or a clear take its turn in the very same history as an
    ordinary draw, rather than living in a slot of its own the way each
    used to.
    """

    kind: str
    index: int
    shape: Shape | tuple[Shape, ...]


class OverlayWindow(QWidget):
    """The overlay redesign's shell: one frameless window spanning the whole
    virtual desktop, per docs/design/overlay-redesign.md.

    Unlike `Overlay` above -- one instance per monitor, selection kept in
    absolute logical virtual-desktop coordinates so per-monitor crops tile
    correctly -- this is a *single* window covering the whole desktop, and
    per the spec's state table (`sel: QRect # window coords`) its selection,
    and every mark in `_marks`, is kept in window coordinates: local to this
    widget's own top-left, not the virtual desktop's. `frame` is expected to
    be a single capture already spanning every monitor -- what
    `BackendRegistry.capture()` returns -- not a per-monitor crop.

    Per CLAUDE.md's one architectural rule, the compositor is asked for
    pixels exactly once, upstream in `capture.py`; the frame handed in here
    is already frozen, and the spec's own deviation note applies: this never
    uses `QScreen.grabWindow(0)`, which returns black on Wayland.

    Three constraints a maintainer must keep intact:

    - Ink stays in window coordinates. `_marks` are stored and painted in
      this widget's own coordinate space, clipped to the selection, per the
      spec's "Ink lives in screen coordinates" -- so re-framing the
      selection moves the clip over marks that never move themselves.
    - The scrim is painted, not stacked. `_paint_scrim` fills the dimmed
      veil directly in this widget's own `paintEvent`, never as a
      translucent child widget layered over the window -- a full-window
      child would sit above the ink layer in z-order and eat its mouse
      events. A single even-odd fill dims the window and punches the
      selection out in one call, so there's
      no separate "dim then punch a hole" step that could disagree with
      itself at the edge.
    - Chrome must never reach the export. `rendered_image()` flattens
      `_marks` onto the selection's crop of the frozen frame; it never
      touches `_bar`, `_style_popover`, `_toast`, `_hud`, `_popover`,
      `_family_menus`, `_close_button` or any other widget painted over
      the overlay, so none of that chrome can ever leak into a copy or a
      save.

    Everything else here is chrome and mode-handling built around those
    three constraints. `FloatingBar` (`_bar`) is the real child widget
    driving undo/redo/clear/copy/save and the active tool; `StylePopover`
    opens over its style dot for whichever tool is active, unless that tool
    has nothing to style. A press
    that misses every resize handle and lands inside the selection starts a
    stroke (`_start_stroke`), drag extends it, and release commits it as a
    mark -- drawn in the active tool's own style from `_styles` -- through
    the same undo/redo/clear history every
    other mutation of `_marks` goes through (`_MarkAction`, folding `add`,
    `erase` and `clear` into one stack so any of them can be undone and
    redone in the order they happened). `CaptureModePopover` picks among
    Region/Window/Full screen/Browser; whichever one produces a selection
    hands it to `set_selection` the same way a plain drag does, so nothing
    downstream needs to know how a selection was produced, and an optional
    countdown delay re-grabs through the same `BackendRegistry` and
    re-opens over the fresh frame in place rather than building a second
    `OverlayWindow`. The two `FamilyMenu`s (`_family_menus`) open from the
    bar's notched slots. `keyPressEvent` wires tool-letter shortcuts from
    `tokens.SHORTCUTS` and the redaction family's key, Ctrl+Z/Ctrl+Shift+Z
    for undo/redo, Enter to
    copy-and-dismiss, 1-7, [, ] and D for the active tool's style
    (`StylePopover.handle_key`), `?` to reveal the hint HUD, and the two-stage Esc
    (`_handle_escape`) the spec leaves for us to decide -- all of it
    suppressed while a slider or a text-editing widget has focus
    (`_shortcuts_suppressed`). `_close_button` (SNX-80) is Esc's visible
    counterpart: a fixed-corner control that discards any ink and closes in
    a single click, shown for as long as this window is regardless of
    `_selection`, active tool or ink state -- unlike `_bar` and the chips,
    which only exist once a selection does.
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

    # Close button (SNX-80): fixed distance from the window's own top-right
    # corner, given as a literal same as every other pixel value above that
    # neither the redesign spec nor this ticket's own handoff tokenized.
    _CLOSE_BUTTON_MARGIN = 16

    # Chips above the selection (SNX-43), per docs/design/overlay-redesign.md's
    # "Chips above the selection" section: the reference gives these as
    # literal CSS values (`padding:6px 11px`, `border-radius:8px`, a 9px/7px
    # flex `gap`) rather than tokens.Metric entries -- same convention as the
    # corner bracket/edge handle constants above. tokens.Metric.CHIP_OFFSET_Y
    # is the one shared value that *is* tokenized, since both chips read it.
    _CHIP_RADIUS = 8
    _CHIP_PAD_V = 6
    _CHIP_PAD_H = 11
    _CHIP_INNER_GAP = 9    # dimension chip: gap between size / dot / mark count
    _CHIP_DOT = "·"   # the reference's bare "·" between size and mark count
    _FROZEN_INNER_GAP = 7  # frozen pill: gap between the pin icon and its label
    _FROZEN_ICON_SIZE = 13
    _FROZEN_LABEL = "Frozen"

    def __init__(
        self,
        frame: Frame,
        parent=None,
        # SNX-65: off by default -- see the "Top hint HUD" comment block
        # above `HintHUD` for why. Still a constructor arg, not a deleted
        # one, so a caller (or a test) that wants the banner from the very
        # first frame still can.
        hints_enabled: bool = False,
        geometry_provider: GeometryProvider | None = None,
        monitor_geometries: list[QRectF] | None = None,
        registry: BackendRegistry | None = None,
        on_dismissed: Callable[[], None] | None = None,
        on_captured: "Callable[[QImage, Path | None], None] | None" = None,
        on_recording_requested: "Callable[[QRectF | None, str, str], None] | None" = None,
        on_recording_start: "Callable[[], None] | None" = None,
    ):
        super().__init__(parent)
        self._frame = frame
        # Fired by `copy()`/`save()` only -- see `_report_capture`.
        self._on_captured = on_captured
        # SNX-122: fired by `_commit_selection`'s record branch, with an
        # absolute-coordinate rect (None for the whole desktop), the armed
        # delay string, and the chooser's after-capture destination
        # ("instant" or "save") -- app.py owns starting/stopping the actual
        # recorder, per CLAUDE.md's split between this file (widget/
        # painting) and app.py (subprocess/filesystem/stateful side
        # effects).
        self._on_recording_requested = on_recording_requested
        self._on_recording_start = on_recording_start
        # SNX-58: called once, from closeEvent, when this window is the
        # Wayland-primary of a multi-monitor `open_overlay` group -- the
        # hook that closes the non-interactive `_MonitorVeil` companions
        # covering the other monitors the moment this one does, so ending
        # the session on the primary never leaves a dimmed veil stuck on
        # another screen. None (the default, and the only value X11 or a
        # single-monitor session ever passes) means there is nothing to
        # close alongside this window.
        self._on_dismissed = on_dismissed

        # SNX-48: sourced for Window/Full screen capture-mode handling
        # below, mirroring `Overlay`'s own constructor args of the same
        # names. `_geometry_provider` defaults the same way `Overlay`'s
        # does -- `UnsupportedGeometryProvider` reports no windows
        # anywhere, which is what makes Window mode degrade instead of
        # needing a None-check at every call site. `_monitor_geometries`
        # defaults to the frame's own full span (there is no per-monitor
        # split to make without one) so a single-monitor caller -- every
        # test in this file included -- gets a correct Full screen
        # selection without having to pass one in.
        self._geometry_provider = geometry_provider or UnsupportedGeometryProvider()
        self._monitor_geometries = (
            list(monitor_geometries)
            if monitor_geometries
            else [QRectF(frame.logical_origin, frame.logical_size)]
        )

        # Deliberately NOT X11BypassWindowManagerHint, tempting though it
        # is: an override-redirect window is not the window manager's to
        # stage, so it would skip Mutter's scale-up-on-map animation --
        # which over a frozen desktop reads as a page expanding across the
        # area being captured. Measured, though, such a window never becomes
        # `_NET_ACTIVE_WINDOW`: the WM hands focus to something else, and
        # this window lives on keyboard input. A cosmetic animation is worth
        # far less than Esc, Enter and every tool letter working.
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
        )
        # Every pixel of this window is the frozen frame, so Qt must not
        # fill it with the palette's background first. That fill is what
        # flashed: the window maps, gets one frame of flat colour, and only
        # then gets its first paintEvent -- read as the screen blinking
        # before the snip rather than the capture simply appearing. Telling
        # Qt the paint is opaque and there is no system background to draw
        # means the first thing ever shown is the desktop itself.
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
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
        # Pinned, or the window manager takes the geometry above as a
        # suggestion and shrinks this window to one monitor's work area.
        # GNOME/Mutter does exactly that to an ordinary managed window:
        # a 6400x1440 request came back as 2560x1337+1920+32, the centre
        # monitor minus the top bar. That is not a cosmetic difference --
        # `paintEvent` draws the frozen frame with
        # `drawImage(QRectF(self.rect()), ...)`, so the whole virtual
        # desktop was then squeezed into whatever the WM allowed, and every
        # coordinate the user dragged in was scaled by the same factor
        # (2.5x horizontally on that layout). The overlay showed all three
        # monitors crushed onto one, and selections came out the wrong size.
        #
        # setFixedSize (min == max size hints), not
        # X11BypassWindowManagerHint: both hold the geometry, but bypassing
        # the WM makes this an override-redirect window that never gets
        # focus from it, and this window lives on keyboard input -- Esc,
        # Enter, Ctrl+Z, every tool letter. Staying managed keeps those
        # working. Wayland is unaffected either way: there
        # `show_on_screen` fullscreens one surface per output instead, and
        # a compositor sizes those itself.
        self.setFixedSize(
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
        # The monitor `_sync_chooser_visibility` last placed the chooser row
        # against, so `_follow_pointer_to_its_monitor` can tell a real
        # crossing from an ordinary move. None until first placed.
        self._chooser_monitor: QRectF | None = None
        # SNX-48: last-known pointer position over the frozen desktop
        # itself (window-local logical coords, the same space `_selection`
        # lives in) -- tracked from ordinary mouse-move events the same
        # way `Overlay._cursor_pos` is, so `_active_screen_rect` can answer
        # "which display is the cursor on" from real moves, and reaches for
        # `QCursor.pos()` -- global state no test can steer offscreen --
        # only before the first one. None until the first move.
        self._cursor_pos: QPointF | None = None
        # True from the moment Window mode is armed (`_enter_window_mode`)
        # until a click lands on a window (`_confirm_window_pick`), per
        # the spec's "hover highlights the window under the cursor...
        # click accepts it." While armed, `mousePressEvent`/
        # `mouseMoveEvent` dispatch to the Window-picking branch ahead of
        # the resize-handle/stroke logic below, the same "handled here,
        # nothing else runs" shape `_active_handle`/`_eraser_active`
        # already use for their own presses.
        self._picking_window = False
        # (title, absolute rect) of the window under the pointer while
        # Window mode is armed, so the preview can name what it would take.
        self._hovered_window: "tuple[str, QRectF] | None" = None
        # Full screen's version of the two above, on a desk with more than
        # one monitor (#53): armed from `_enter_monitor_mode` until a click
        # lands on a monitor (`_confirm_monitor_pick`), with
        # `_hovered_monitor` the absolute rect the preview highlights.
        # Unlike Window mode's hover the preview is never a selection --
        # nothing is chosen until the click -- so the chooser tab stays up
        # and keeps following the pointer.
        self._picking_monitor = False
        self._hovered_monitor: QRectF | None = None

        # Handle currently being dragged (SNX-33 re-framing), and the
        # selection as it stood the moment that drag started. The anchor is
        # read-only for the drag's whole duration -- every edge it doesn't
        # own comes from here, never from the live selection -- which is
        # what keeps the opposite edge/corner from creeping as the mouse
        # moves. None outside a handle drag.
        self._active_handle: Handle | None = None
        self._resize_anchor: QRect | None = None

        # True for the duration of an eraser press -- see mousePressEvent.
        self._erasing = False

        # Region-mode drag-to-create (SNX-57): window-local logical anchor
        # of an in-progress left-button drag that is building a *brand new*
        # selection from nothing, as opposed to `_active_handle`'s resize of
        # an existing one. None outside such a drag. Only ever armed from
        # `mousePressEvent` when a press misses every handle and there is no
        # selection yet -- Window and Full screen each already produce their
        # own first selection through `_confirm_window_pick`/
        # `_select_full_screen`, so this is what gives
        # Region -- the default mode, with no picking flag of its own --
        # the same "drag on an empty overlay" starting point the others get
        # for free.
        self._region_drag_anchor: QPointF | None = None

        # Where the current selection's drag *began*, window-local, kept
        # after the drag ends -- unlike `_region_drag_anchor`, which is
        # armed only for the duration of one. `_chrome_bounds` prefers
        # this point's monitor, so a selection dragged across a bezel
        # keeps its toolbar on the monitor the user started on rather
        # than having it jump to whichever monitor ended up with a few
        # more pixels of it. None when the selection came from
        # somewhere other than a drag (Window/Full screen), where the
        # picked rect's own monitor is the better answer.
        self._selection_anchor: QPointF | None = None

        # The ink layer (SNX-34): overlay-window coordinates, the same
        # space `_selection` lives in above -- never translated relative to
        # the selection, per the class docstring. Paint order is the list
        # order, mirroring shapes.py's render().
        # The ink layer and its history now live in a MarkStore, which the
        # review window's Annotate mode drives too -- see snipux/marks.py.
        # `_marks` stays as a read-only view so this file's many painting
        # and hit-testing sites read exactly as they did.
        self._mark_store = MarkStore(self)
        self._mark_store.changed.connect(self._on_marks_changed)
        # SNX-63: cached output of `_base_layer_image` -- `(key, image)`,
        # or None before the first paint -- so a repaint triggered by
        # something unrelated to any committed `ObscuringShape` (an
        # in-progress pen stroke elsewhere, a resize drag, marching ants)
        # does not redo blur/pixelate's scale-down-then-up sampling every
        # single frame. See that method's own docstring for what `key`
        # covers and why equality against it is enough to know the cached
        # image is still correct.
        self._base_layer_cache: tuple[tuple, QImage] | None = None
        # Undo/redo history (SNX-39; SNX-70 folds the eraser into it): a
        # stack of `_MarkAction`s, one per `add_mark`/`erase_at` call, in
        # the order they happened. `undo()` pops `_undo`, inverts the
        # action (dropping an 'add', reinserting an 'erase') and pushes it
        # onto `_redo`; `redo()` pops `_redo`, replays the action
        # (reinserting an 'add', re-removing an 'erase') and pushes it back
        # onto `_undo`. Recording each action's `index` at the moment it
        # ran is what lets an 'erase' from the middle of `_marks` -- not
        # just the newest mark -- undo/redo back to exactly the position it
        # happened at, the same way an 'add' 's end-of-list position always
        # has.


        # Eraser tool state. False until the floating bar's eraser button
        # (via `_on_tool_selected`) arms it through `set_eraser_active` --
        # marks are only ever hit-tested while this is True, per
        # docs/design/overlay-redesign.md's "Drawing": "hit-testable only
        # while the eraser is active," so ordinary drawing never pays for it.
        self._eraser_active = False

        # Live drawing (SNX-52): the mark a left-press/drag/release is
        # currently building, in this widget's own window coordinates --
        # the same space `_marks` lives in, per the class docstring. None
        # outside an active stroke; `mousePressEvent` never sets both this
        # and `_active_handle` for the same press, so a resize and a
        # stroke can never be in progress at once. See `_start_stroke`.
        self._in_progress_shape: Shape | None = None
        # Monitor top-left -> logical pixels of that monitor's top edge the
        # desktop's own chrome owns. Cached because `_chrome_bounds` runs at
        # mouse-move frequency and the Linux answer shells out; the desktop's
        # panels do not move mid-snip, and a snip is seconds long.
        self._reserved_margins_cache: dict[tuple[int, int], QMargins] = {}

        # The text tool (SNX-52): a lazily-built QLineEdit that mirrors
        # editor.py's `Canvas._ensure_text_edit`/`_commit_text` -- a click
        # opens it, seeded with a placeholder (never pre-filled text, so
        # committing with nothing typed still discards it, per
        # `_commit_text`'s own guard) and focused for immediate typing.
        # `_pending_text_*` holds the press-time colour/stroke/point until
        # `editingFinished` commits (or discards) them; `_committing_text`
        # guards against `hide()`'s own re-entrant `editingFinished`, same
        # as `Canvas._commit_text`'s own docstring explains.
        # The text tool's label editor, shared with the review window's
        # Edit mode -- see snipux/marks.py. Marks and host coordinates are
        # the same space here, so it needs no mapping.
        self._text_editor = TextLabelEditor(self, self._mark_store)

        self._dash_offset = 0.0
        self._ants_timer = QTimer(self)
        self._ants_timer.setInterval(self._ANTS_TIMER_INTERVAL_MS)
        self._ants_timer.timeout.connect(self._advance_ants)

        # The floating bar (SNX-40): a real child widget, positioned under
        # the selection by `_sync_bar` below rather than drawn in this
        # class's own paintEvent. Starts hidden -- shown only once this
        # window itself is shown (see showEvent/hideEvent) so a caller that
        # never shows the window (most of this file's own tests, which grab()
        # an unshown widget to sample pixels) never has the bar painted over
        # whatever they're sampling.
        self._bar = FloatingBar(self)
        self._bar.hide()
        self._bar.undoRequested.connect(self.undo)
        self._bar.redoRequested.connect(self.redo)
        self._bar.clearRequested.connect(self.clear)
        self._bar.copyRequested.connect(self._on_bar_copy)
        self._bar.saveRequested.connect(self._on_bar_save)
        self._bar.openRequested.connect(self._on_bar_open)
        self._bar.destinationMenuRequested.connect(self._open_destination_menu)
        self._bar.toolSelected.connect(self._on_tool_selected)
        # Bound, not a lambda: a lambda holding this window, kept by a bar
        # this window owns, is a cycle Python cannot see into, and the
        # window -- frozen frame and all -- would never be freed.
        self._bar.toolPicked.connect(self._on_tool_picked)
        self._bar.familyMenuRequested.connect(self._toggle_family_menu)
        self._bar.styleRequested.connect(self._toggle_style)
        # #50: the bar can be dragged, and where it was dragged to when a
        # selection left it no room is where it goes the next time one does.
        # A bad stored value reads as None, and None is automatic placement.
        self._bar.set_remembered_spot(setup_desktop.load_bar_position())
        self._bar.dragStarted.connect(self._on_bar_drag_started)
        self._bar.dragMoved.connect(self._on_bar_dragged)
        self._bar.spotRemembered.connect(self._remember_bar_spot)

        # Each tool's style -- the session's, so what was set on the last
        # snip is still set on this one -- and the popover the style dot
        # opens to change it. Its keys work whether it is open or not; see
        # `StylePopover`. It is one of the bar's menus: one of those is open
        # at a time.
        self._styles: ToolStyles = session_styles
        self._style_popover = StylePopover(self._styles, self)
        self._style_popover.hide()
        self._style_popover.styleChanged.connect(self._on_style_changed)
        self._style_popover.openChanged.connect(self._on_style_open_changed)

        # Names the active tool and what it does, so there is never an
        # active tool with nothing on screen identifying it. See
        # `ToolHintStrip`.
        self._tool_hint = ToolHintStrip(self)
        self._tool_hint.hide()

        # The pre-snip chooser: what to capture, and what should happen to
        # it. Shown while there is no selection, which is exactly when the
        # floating bar is not -- the two never share the screen. See
        # `CaptureChooser` for why this exists at all.
        # The handoff's own surface -- see snipux/chooser.py and
        # docs/design/handoff-chooser.md. It owns mode, destination and
        # delay; this window owns everything downstream of them.
        self._chooser = Chooser(self)
        self._chooser.modeChosen.connect(self._on_chooser_mode)
        self._chooser.fireImmediately.connect(self._on_chooser_immediate)
        self._chooser.cancelled.connect(self._cancel)
        self._chooser.set_after(setup_desktop.load_after_capture())
        self._chooser.set_record_after_default(setup_desktop.load_recording_after())
        # Connected *after* the two seeds above, not before: seeding is this
        # window adopting what is already stored, and a connection made
        # first would answer it by writing the same value straight back.
        # Only a real change from here on is the user choosing something.
        self._chooser.afterChanged.connect(self._remember_destination)
        # The reuse toggle lives on the row, not in Settings -- see
        # `chooser._RowToggle`. Seeded here (which never emits) and
        # persisted on every real click, the same shape `kind` uses below.
        self._chooser.set_reuse_last_region(setup_desktop.load_reuse_last_region())
        # `Tab` is greyed unless a browser page can actually be found. Asked
        # once, here, rather than on every menu open: enumerating windows
        # walks the whole desktop, and the answer cannot change while a
        # frozen frame is on screen.
        self._browser_tab = self._geometry_provider.browser_viewport()
        self._chooser.set_browser_available(self._browser_tab is not None)
        # Active window is asked here for the same reason, and for one that
        # decides whether it works at all: this window is not on screen yet,
        # so the focused window is still the one the user was in. Once the
        # chooser is up, snipux is the focused window, and asking when the
        # row is picked could only ever find it.
        self._active_window = self._geometry_provider.active_window()
        self._chooser.set_active_window_available(
            self._active_window is not None,
            design.tokens.ACTIVE_WINDOW_UNAVAILABLE
            if self._geometry_provider.is_available()
            else design.tokens.ACTIVE_WINDOW_UNSUPPORTED,
        )
        # Whether Full screen fires on the pick or arms (#53): the monitors
        # this window covers, which on Wayland with several is only the
        # interactive one -- the others cannot be offered from here.
        self._chooser.set_monitor_count(len(self._monitor_geometries))
        self._chooser.reuseLastRegionChanged.connect(
            setup_desktop.save_reuse_last_region
        )
        # Hide sensitive: seeded and persisted the way the reuse toggle is,
        # and greyed on a machine that cannot read text out of an image.
        self._chooser.set_hide_sensitive(setup_desktop.load_hide_sensitive())
        self._chooser.set_hide_sensitive_available(
            platform.current.recognizes_text(),
            platform.current.text_recognition_unavailable_reason(),
        )
        self._chooser.hideSensitiveChanged.connect(setup_desktop.save_hide_sensitive)
        # `kind` (the stills/record switch) has no Settings surface the way
        # `after` does -- the chooser itself is the only place it is ever
        # set, so it is loaded the same way but persisted on every change
        # rather than only read here. See `setup_desktop.load_kind`.
        self._chooser.set_kind(setup_desktop.load_kind())
        self._chooser.kindChanged.connect(setup_desktop.save_kind)
        self._chooser.hide_all()

        # The capture-mode popover: opened from the bar's chip click via
        # `_toggle_capture_popover`, positioned by the popover's own
        # `reposition` against `_bar`'s geometry. `_capture_mode`/`_delay`
        # start at `tokens.CAPTURE_MODES`/`tokens.DELAYS`' own first
        # entries; `_dispatch_capture_mode`/`_start_delayed_capture` are
        # what read them back, and the chip's own label is kept in sync by
        # `_on_capture_mode_selected`.
        self._capture_mode: str = design.tokens.CAPTURE_MODES[0][0]
        # True between committing a record selection and app.py starting
        # the backend: the window stays up so the region can still be
        # reframed, with the stills bar suppressed. See `_commit_selection`.
        self._armed_for_recording = False
        # Latch for `_arm_default_tool`: the pen is armed once, the first
        # time this snip's toolbar appears, and never again -- see there.
        self._armed_default_tool = False
        # True while `_selection` is a rectangle this window recalled by
        # itself (`_preselect_last_region`) and the user has not yet
        # adopted it by doing anything to it. It is the one selection
        # nobody asked for, so until it is adopted a press anywhere reframes
        # rather than draws, and no tool is armed over it -- see
        # `_adopt_selection`.
        self._recalled_selection = False
        self._delay: str = design.tokens.DELAYS[0]
        self._popover = CaptureModePopover(self)
        self._popover.hide()
        self._popover.modeSelected.connect(self._on_capture_mode_selected)
        self._popover.delayChanged.connect(self._on_delay_changed)
        # The row's delay is the same delay (#73). Connected here rather than
        # beside the chooser's other signals, because `_delay` and
        # `_popover` have to exist before the first change can land.
        self._chooser.delayChanged.connect(self._on_delay_changed)
        self._bar.captureChipClicked.connect(self._toggle_capture_popover)

        # The notched slots' menus, one per family -- see `FamilyMenu` for
        # why they belong to this window rather than to the bar. One is open
        # at a time, and the style popover counts as one.
        self._family_menus = {
            family: FamilyMenu(family, self) for family in design.tokens.FAMILIES
        }
        for menu in self._family_menus.values():
            menu.hide()
            menu.siblingPicked.connect(self._on_family_sibling_picked)
        # Hovering a tool names it -- see `ToolHintStrip`. Not Qt's tooltip,
        # which on an always-on-top frameless window is a coin toss.
        self._bar.toolHovered.connect(self._preview_tool)
        self._bar.toolUnhovered.connect(self._sync_tool_hint)

        # The delayed re-capture (SNX-50): `_registry` is what
        # `_finish_delayed_capture` re-grabs through -- an empty
        # `BackendRegistry` by default, the same "an inert default degrades
        # instead of needing a None-check everywhere" shape
        # `UnsupportedGeometryProvider` already gives `_geometry_provider`
        # above (an empty registry's own `capture()` raises `CaptureError`
        # with its own "no capture backend is available" message, which
        # `_finish_delayed_capture` already has to handle for a real,
        # non-empty registry that simply fails). `_countdown`/`_delay_timer`
        # are built lazily, on the first delay actually confirmed, rather
        # than unconditionally here -- unlike `_toast`/`_hud`, `_countdown`
        # is not a child of this window (see its own docstring for why) and
        # every `OverlayWindow` this file's tests build would otherwise
        # leave a real, if hidden, extra top-level widget behind it.
        # `_pending_capture_mode` is the mode `_on_capture_mode_selected`
        # was confirming when the delay started, re-dispatched once the
        # fresh frame is in.
        self._registry = registry if registry is not None else BackendRegistry()
        self._countdown: DelayCountdown | None = None
        self._delay_timer: QTimer | None = None
        self._delay_remaining = 0
        self._pending_capture_mode: str | None = None

        # The toast (SNX-45): the single `Toast` instance `copy`/`save`/
        # `clear`/`discard` below all share -- see `_show_toast` for the
        # `self.isVisible()` gate that keeps it from painting into this
        # window's own many pixel-sampling tests, none of which call
        # `.show()`, the same reason `_sync_bar_visibility` gates `_bar`.
        self._toast = Toast(self)


        # The top hint HUD (SNX-46): behind the `hints` preference the spec
        # puts it behind -- SNX-65 changed the default to off. A real child
        # widget the same way `_bar`/`_toast` are -- see `_sync_hud_visibility`
        # for the same `self.isVisible()` gate those two already use, here
        # paired with `_hints_enabled` so turning the preference off hides
        # the bar immediately regardless of this window's own visibility.
        # Spans the window's full width at construction time -- this
        # window's own geometry is set once above and never resized
        # afterwards (a fullscreen overlay), so there is no resizeEvent to
        # keep this in sync with.
        self._hints_enabled = hints_enabled
        self._hud = HintHUD(self)
        self._hud.setGeometry(0, 0, self.width(), design.tokens.Metric.HUD_H)
        self._hud.hide()

        # The close button (SNX-80): see `_CloseButton`'s own docstring and
        # the "Close button" comment block above it for why this exists at
        # all, and why it is neither a bar button nor per-selection chrome.
        # Positioned once, here, the same way `_hud` above is -- this
        # window's own geometry is set once at construction and never
        # resized afterwards (a fullscreen overlay), so there is no
        # resizeEvent to keep a fixed corner offset in sync with. Starts
        # hidden, like `_bar`/`_style_popover`/`_toast`/`_hud`, so a caller that
        # never shows this window (most of this file's own tests, which
        # `grab()` an unshown widget to sample pixels) never has it painted
        # over whatever they're sampling; `showEvent`/`hideEvent` are what
        # bring it up and down with the window itself, unconditionally --
        # unlike every other piece of chrome here, its visibility never
        # depends on `_selection`, `_bar.active_tool` or `_marks`.
        self._close_button = _CloseButton(self)
        self._close_button.clicked.connect(self._cancel)
        self._close_button.hide()
        self._reposition_close_button()

        # Last, once every piece of chrome above exists to react to it: the
        # overlay opens on the previous snip's rectangle when the
        # preference asks for it. Region is the mode a fresh overlay starts
        # in (`tokens.CAPTURE_MODES[0]`), so this is that mode's opening
        # state rather than a separate step -- and it is a no-op for
        # everyone who has left the preference off.
        #
        # `_sync_bar_visibility` is gated on `self.isVisible()`, which is
        # still false here, so the bar catches up in `showEvent` -- which
        # already handles exactly this case for the same reason.
        self._preselect_last_region()

    def _remember_destination(self, after: str) -> None:
        """Persist the destination the user just picked, so the next snip
        opens on it.

        The handoff's rule was that the per-snip control never writes back
        -- Settings held the destination, and the chooser and the split
        button's caret were both one-snip overrides. In practice that read
        as the control being broken: picking Copy, taking the snip, and
        finding the next one back on Open again looks like the choice was
        ignored, and it is the second-most-changed setting in the app after
        the mode itself. Reported as "how come this isnt saving my last
        selected option, like i picked copy?". Last-used-wins is the rule
        now, and the Settings radio is a starting point rather than a
        fixed default -- it will show whatever was last chosen here.

        The two sides keep their own answer. `after` on the record side is
        drawn from a different vocabulary (`tokens.RECORD_AFTER_ROWS`:
        Copy/Save/Open) and has its own stored key, so writing a recording
        destination into the stills one would leave the stills chooser
        seeded from a value its own menu cannot show.
        """
        if self._chooser.kind == "record":
            setup_desktop.save_recording_after(after)
        else:
            setup_desktop.save_after_capture(after)

    def set_selection(self, rect: QRect | None) -> None:
        """Set the current selection (window coordinates) and repaint."""
        if rect is None:
            self._selection_anchor = None
        # Every other route to a selection is one the user asked for, so
        # the recall flag is cleared here and re-set by
        # `_preselect_last_region` alone. The browser flag goes the same
        # way: framing anything by hand means the bar's actions are about
        # that rectangle again, not about a page.
        self._recalled_selection = False
        self._selection = rect
        self._sync_bar_visibility()
        self._sync_chooser_visibility()
        # Follows the selection onto its monitor, like every other piece of
        # chrome -- see `_reposition_close_button`.
        self._reposition_close_button()
        self.update()

    def _on_tool_selected(self, tool: str) -> None:
        """Wire the bar's tool buttons to the eraser's hit-testing
        arm/disarm (see `set_eraser_active`), the style dot and popover, and
        the strip naming the tool. `mousePressEvent`/`_start_stroke` read `self._bar.
        active_tool` directly at press time rather than this class keeping
        a second copy of it -- `FloatingBar` is already the one place a
        click and a shortcut key (`keyPressEvent`'s tool letters) both
        funnel through (`FloatingBar.select_tool`'s own docstring), so
        there is nothing for this method to track beyond the two things
        below that don't already live on the bar.
        """
        # Reaching for a tool is adopting whatever is framed: from here on
        # a press inside a recalled region draws rather than reframing,
        # which is the whole point of having picked one.
        self._recalled_selection = False
        self.set_eraser_active(tool == "eraser")
        for menu in self._family_menus.values():
            menu.hide()
        self._follow_tool_with_style(tool)
        self._sync_tool_hint()

    def _follow_tool_with_style(self, tool: str | None) -> None:
        """Point the style dot and the popover at `tool`.

        An open popover follows a tool changed by key -- a key dismisses
        nothing -- and closes for a tool with nothing to style. A tool
        clicked on the bar has already closed it (`_on_tool_picked`).
        """
        self._style_popover.set_tool(tool)
        self._bar.set_style_preview(self._styles.of(tool))
        if self._style_popover.isHidden():
            return
        if design.tokens.STYLE_SECTIONS.get(tool):
            # Another tool's sections can make it another height.
            self._place_style_popover()
        else:
            self._style_popover.hide()

    def _on_style_changed(self, tool: str) -> None:
        """A pick or a style key: the dot shows it at once."""
        if tool == self._bar.active_tool:
            self._bar.set_style_preview(self._styles.of(tool))

    def _on_style_open_changed(self, open_: bool) -> None:
        self._bar.set_style_open(open_)
        self._sync_tool_hint()

    # -- capture-mode popover (SNX-44) --------------------------------------

    def _toggle_capture_popover(self) -> None:
        """Open/close the capture-mode popover from the bar's chip click,
        per the spec's "The chip is a mode selector... Opens the popover."

        A second click while it's already open closes it again, mirroring
        the reference's own `toggleMode: () => this.setState({ modeOpen:
        !st.modeOpen })` -- there is no other way to close it from the chip
        itself once it's open.
        """
        if self._popover.isVisible():
            self._popover.hide()
            return
        self._popover.reposition(self._bar.geometry(), self._chrome_bounds())
        self._popover.show()
        self._popover.raise_()

    # -- the bar's menus -----------------------------------------------------

    def _toggle_family_menu(self, family: str) -> None:
        """Open `family`'s menu from its slot's notch, or close it if it is
        the one already open.

        Seeded with the sibling the slot shows, and anchored to that slot in
        this window's coordinates -- the slot's own geometry is the bar's.
        """
        menu = self._family_menus[family]
        # Hidden, not visible: a window not yet on screen has no visible
        # children, and its menu would reopen on every toggle.
        if not menu.isHidden():
            menu.hide()
            return
        self._close_bar_menus()
        menu.set_current(self._bar.family_choice(family))
        menu.reposition(
            self._bar.slot_rect(family, self), self._bar.geometry(), self._chrome_bounds()
        )
        menu.show()
        menu.raise_()

    def _on_family_sibling_picked(self, tool: str) -> None:
        """A row picked in a family menu: the same path a click on a slot
        takes -- whatever else is open closes -- then the sibling is armed.
        """
        self._close_bar_menus()
        self._bar.select_tool(tool)

    def _on_tool_picked(self, _tool: str) -> None:
        """A tool clicked on the bar closes whatever menu is open."""
        self._close_bar_menus()

    def _close_bar_menus(self) -> bool:
        """Close the family menus and the style popover, and say whether any
        of them was open.
        """
        was_open = not self._style_popover.isHidden() or any(
            not menu.isHidden() for menu in self._family_menus.values()
        )
        for menu in self._family_menus.values():
            menu.hide()
        self._style_popover.hide()
        return was_open

    def _toggle_style(self) -> None:
        """The style dot: open the popover for the active tool, or close it.

        Picks inside it never close it -- only this, Esc, a press outside
        it, or another menu opening.
        """
        if not self._style_popover.isHidden():
            self._style_popover.hide()
            return
        tool = self._bar.active_tool
        if not self._bar.isVisible() or not design.tokens.STYLE_SECTIONS.get(tool):
            return
        self._close_bar_menus()
        self._style_popover.set_tool(tool)
        self._place_style_popover()
        self._style_popover.show()
        self._style_popover.raise_()

    def _place_style_popover(self) -> None:
        self._style_popover.reposition(
            self._bar.style_dot_rect(self), self._bar.geometry(), self._chrome_bounds()
        )

    # -- dragging the bar (#50) ------------------------------------------------

    def _on_bar_drag_started(self) -> None:
        """A drag of the bar is a press outside every menu and the style
        popover, so it closes them the way a press on the frame does.

        Closed rather than carried along: each is anchored to the control it
        opened from, and one left open over a moving bar would be
        re-flipping above and below it for the length of the drag. Only
        once the pointer has moved past the drag threshold, so a press on
        the bar's padding that goes nowhere still changes nothing.
        """
        self._popover.hide()
        self._close_bar_menus()

    def _on_bar_dragged(self) -> None:
        """What still hangs off the bar -- the strip naming the tool --
        follows it, clamped into `_chrome_bounds` as always.
        """
        self._sync_tool_hint()

    def _remember_bar_spot(self, x: float, y: float) -> None:
        """Keep where the bar was dragged to for the next snip with no room,
        as the fraction of its travel `FloatingBar.spot` describes -- not
        pixels, so it still means somewhere on a monitor of another size.
        """
        setup_desktop.save_bar_position((x, y))

    _CHOOSER_TOP_MARGIN = 28

    def _on_capture_mode_selected(self, mode: str) -> None:
        """Track the popover's chosen capture mode and update the chip's
        own label to match, per the reference's `{{ mode }}` binding on
        the chip button itself.

        SNX-48 makes Window and Full screen actually do something past
        the label update: `_enter_window_mode` arms hover-preview/click-
        to-snap picking, `_select_full_screen` snaps `_selection`
        immediately. Whatever picking was in progress for a previous mode
        is disarmed unconditionally first -- switching away from Window
        mid-pick must not leave `_picking_window` stuck armed underneath
        whatever the newly-picked mode does instead.

        SNX-50 (this ticket) intercepts all of the above whenever `_delay`
        isn't `Off`: per the spec's Delay entry, confirming a mode while a
        delay is set dismisses the overlay, waits, re-grabs and re-opens,
        rather than acting on the current (soon to be stale) frame right
        away. `_start_delayed_capture` takes over from here and
        re-dispatches to this same mode logic itself, once the fresh frame
        is in.
        """
        self._picking_window = False
        self._picking_monitor = False
        self._hovered_monitor = None
        self.update()
        # SNX-57: a mode switch mid-drag must not leave this armed under
        # whatever the newly-picked mode does instead.
        self._region_drag_anchor = None
        self._capture_mode = mode
        self._bar.set_capture_mode(mode)
        # `arm=False`: this is the two surfaces agreeing on one value, not
        # a fresh choice. Arming here would re-emit `modeChosen` straight
        # back into this method -- one piece of state, two surfaces, and
        # exactly one of them originating each change.
        self._chooser.set_mode(mode, arm=False)

        if self._delay != design.tokens.DELAYS[0]:
            self._start_delayed_capture(mode)
            return

        self._dispatch_capture_mode(mode)

    def _dispatch_capture_mode(self, mode: str) -> None:
        """Run whichever mode-specific picking `mode` itself calls for --
        the immediate half of `_on_capture_mode_selected`, factored out so
        `_finish_delayed_capture` can re-run it against a fresh frame
        without also re-running the delay check above (which would just
        recurse into another wait).
        """
        if mode == "Window":  # design.tokens.CAPTURE_MODES[1][0]
            self._enter_window_mode()
        elif mode == "Full screen":  # design.tokens.CAPTURE_MODES[2][0]
            # One monitor leaves nothing to choose; more leave which one
            # (docs/design/bars/divergences.md 3) -- the line
            # `Chooser._fires_immediately` draws too.
            if len(self._monitor_geometries) > 1:
                self._enter_monitor_mode()
            else:
                self._select_full_screen()
        elif mode == design.tokens.BROWSER_MODE:
            self._select_browser_tab()
        elif mode == design.tokens.ACTIVE_WINDOW_MODE:
            self._select_active_window()
        elif mode == "Region":  # design.tokens.CAPTURE_MODES[0][0]
            self._preselect_last_region()
        # handoff-chooser.md, Armed: "The cursor becomes a crosshair." The
        # Window branch above repaints it on every move, but
        # Region has nothing to preview and would otherwise sit under a
        # plain arrow until the drag it is waiting for actually starts --
        # which is the one moment the pointer most needs to say "drag me".
        self._apply_idle_cursor()

    def _apply_idle_cursor(self) -> None:
        """The pointer when nothing is being dragged, resized or hovered.

        Armed with no selection yet means the chooser has stepped aside and
        the whole monitor is the target, so the crosshair is the invitation.
        While still choosing it stays an ordinary arrow: the panel is a
        thing to click, not an area to drag across.
        """
        if self._selection is None and self._chooser.phase == "armed":
            self.setCursor(Qt.CursorShape.CrossCursor)
        else:
            self.unsetCursor()

    # -- delayed re-capture (SNX-50) -----------------------------------------
    # docs/design/overlay-redesign.md's "Capture modes" entry for Delay is
    # the authority: "Off / 3s / 5s / 10s. When set, the overlay dismisses,
    # waits, re-grabs and re-opens. Show a countdown." The re-grab is a
    # second, independent call through `_registry` -- the same
    # `BackendRegistry` the first frame came through, per CLAUDE.md's one
    # architectural rule applying to this grab exactly as it does to the
    # first -- never a re-use of `_frame` as it stood before the wait.

    def _start_delayed_capture(self, mode: str) -> None:
        """Hide this window and start counting the seconds in `_delay`
        down, per the spec's "the overlay dismisses, waits" -- hiding
        happens synchronously, before the wait itself begins, so the
        overlay is never on screen (and never in danger of being in its
        own next screenshot) for any part of the delay.

        `mode` is remembered as `_pending_capture_mode` and re-dispatched
        by `_finish_delayed_capture` once the fresh frame is in, so a
        delayed Window/Full screen pick still does its own thing
        against the new content instead of only ever landing on Region.
        """
        self._pending_capture_mode = mode
        self._delay_remaining = int(self._delay.rstrip("s"))
        self.hide()

        if self._countdown is None:
            self._countdown = DelayCountdown()
        self._countdown.set_seconds_remaining(self._delay_remaining)
        # On the monitor being worked on, not the middle of the virtual
        # desktop this window spans: on a staggered desk that middle can be
        # a gap, and with a monitor mounted above two others it is the one
        # corner all three share, splitting the countdown across them.
        # Absolute, because the countdown is a top-level window of its own.
        self._countdown.show_centered_on(self._chrome_monitor().toRect())

        if self._delay_timer is None:
            self._delay_timer = QTimer(self)
            self._delay_timer.setInterval(1000)
            self._delay_timer.timeout.connect(self._tick_delay)
        self._delay_timer.start()

    def _tick_delay(self) -> None:
        """One second of the countdown elapsing. Reaching zero stops the
        timer and hands off to `_finish_delayed_capture` -- this method
        itself never touches `_registry` or `_frame`.
        """
        self._delay_remaining -= 1
        if self._delay_remaining <= 0:
            self._delay_timer.stop()
            self._countdown.hide()
            self._finish_delayed_capture()
            return
        self._countdown.set_seconds_remaining(self._delay_remaining)

    def _finish_delayed_capture(self) -> None:
        """Re-grab through `_registry` and re-open over the fresh frame.

        A failed re-grab (per CLAUDE.md, a capture failure must not take
        down the rest of the app) leaves the *old* frame in place and
        simply re-shows this window with it, toasted, rather than leaving
        the user with no overlay and no explanation at all -- mirrors
        `_enter_window_mode`'s own "toast and fall back" handling of an
        unavailable `GeometryProvider`.

        On success, `_frame` and this window's own geometry are replaced
        in place (this is still the same `OverlayWindow`, never a second
        one) -- which is what leaves each tool's style, the
        bar's active tool and `_capture_mode` itself exactly as the user had
        them, with nothing to copy across, per the ticket's "the overlay
        re-opens... with the tool, colour and stroke settings the user had
        chosen" acceptance criterion. The stale selection is cleared --
        it described a rectangle of the *old* content -- and the picked
        mode is re-dispatched against the new one.
        """
        mode = self._pending_capture_mode
        try:
            frame = self._registry.capture()
        except CaptureError as exc:
            self.show()
            self._show_toast("timer", str(exc))
            return

        if mode == design.tokens.ACTIVE_WINDOW_MODE:
            # A delay exists so the screen can change before the shot, and
            # which window has focus is part of what can change. So it is
            # asked again here, while this window is still hidden, to match
            # the fresh frame. The opening answer is kept when nothing can
            # be named this instant: the countdown was hidden a moment ago
            # and focus may not have settled back yet.
            self._active_window = (
                self._geometry_provider.active_window() or self._active_window
            )

        self._frame = frame
        self.setGeometry(
            round(frame.logical_origin.x()),
            round(frame.logical_origin.y()),
            round(frame.logical_size.width()),
            round(frame.logical_size.height()),
        )
        self._hud.setGeometry(0, 0, self.width(), design.tokens.Metric.HUD_H)
        self.set_selection(None)
        self.show()
        self._dispatch_capture_mode(mode)

    # -- Window / Full screen capture modes (SNX-48) -------------------------
    # docs/design/overlay-redesign.md's "Capture modes" section is the
    # authority: "Window -- hover highlights the window under the cursor
    # (snap the selection to its frame); click accepts it. Then annotation
    # proceeds identically" and "Full screen -- selection = the whole
    # display." Both mirror `Overlay`'s own WINDOW/FULL_SCREEN handling
    # above, but hand their result to this window's own `set_selection`
    # instead of emitting `confirmed` into a separate editor -- the whole
    # point of this ticket is that the result stays open for re-framing
    # and in-place annotation exactly like a dragged selection.

    def _enter_window_mode(self) -> None:
        """Arm Window-mode picking, or -- per the ticket's acceptance
        criterion -- tell the user and fall back to Region if this
        platform has no `GeometryProvider` that can answer at all.

        A silent no-op here (the trap `UnsupportedGeometryProvider`'s own
        docstring warns "isn't load-bearing for the mode-3 fallback
        itself" against) would leave the chip reading "Window" for a mode
        that can never produce anything -- indistinguishable from a bug.
        Instead this toasts an explanation and reverts `_capture_mode`,
        the chip label, the popover's own selected row and the chooser
        back to Region, mirroring `CaptureModePopover.set_mode`'s own
        "seeding the popover from elsewhere" use case.

        The chooser is in that list because it is a third surface showing
        the same value: without it the tab read "Window" -- a mode that had
        just been refused -- while the chip beneath read "Region".
        `arm=False` for the same reason `_on_capture_mode_selected` uses
        it, one surface originating each change.
        """
        if not self._geometry_provider.is_available():
            self._show_toast("window", "Window capture isn't available on this session")
            fallback = design.tokens.CAPTURE_MODES[0][0]  # "Region"
            self._capture_mode = fallback
            self._bar.set_capture_mode(fallback)
            self._popover.set_mode(fallback)
            self._chooser.set_mode(fallback, arm=False)
            return
        self._picking_window = True
        # Whatever was selected before (if anything) is not a Window-mode
        # preview and must not linger on screen while the user hasn't
        # hovered a window yet -- mirrors `Overlay`'s own hover branch,
        # which likewise clears on a miss rather than leaving a stale rect.
        self.set_selection(None)

        self._sync_chooser_visibility()

    def _commit_selection(self, rect) -> None:
        """A selection stops being provisional here.

        Each capture mode arrives by its own route -- a region drag's
        release, a click on a window, Full screen's immediate snap -- and
        this is the one moment they all agree the
        user has actually chosen something. `instant` (`tokens.
        AFTER_CAPTURE`) finishes the snip from here, which is why it needs
        a funnel of its own rather than hanging off `set_selection`: that
        one also runs on every mouse-move of a live drag, and finishing on
        the first pixel of a drag is not what "instant" means.

        Identical to Copy on the floating bar, deliberately -- same
        `copy()`, same dismissal, same toast. Instant is not a second way
        to finish a snip, it is the same one with nothing in front of it.

        `load_instant_saves` (SNX-111) is the one thing that turns this
        into Save instead: `edit`/`review` already have a bar to press
        Copy or Save on, but `instant` skips the bar entirely, so without
        this check it could never do anything but copy -- the "Save
        silently" destination the old three-way menu had lost.
        """
        self.set_selection(rect)
        if self._chooser.kind == "record":
            # Recording has no annotate-in-place and no bar to press Copy
            # or Save on (docs/design/recording.md: "There is no
            # annotate-in-place for a video"), so the stills bar stays
            # hidden -- but the window itself stays *up*, unlike every
            # other branch here.
            #
            # It has to. The handoff's ready stage is the one place a
            # recording can still be reframed ("Reframe now -- you cannot
            # resize once it is rolling"), and the handles that do the
            # reframing are this window's. Closing here left the user with
            # a Record button, no visible region and nothing to drag:
            # "i dragged a region and it says i can record but i dont know
            # where its recording? I cant resize or make adjustments?"
            #
            # Nothing is being filmed yet, so a frozen frame is the right
            # thing to be looking at. `app.py` closes this window at the
            # moment recording actually starts, which is the moment a
            # frozen frame would start being filmed instead -- see
            # docs/design/flow/divergences.md 4.
            # Full screen means *this monitor*, the same thing it means on
            # the stills side and the same thing the chooser promises
            # ("Grabs this monitor the moment you choose it"). It used to
            # hand over None, which the backend reads as the whole virtual
            # desktop -- so choosing it on a three-monitor machine produced
            # one 6400x1440 video of all three, while the identical row on
            # the stills side captured one display. Full screen has already
            # set `rect` to one display -- the one under the cursor or, on a
            # desk with several, the one clicked (#53) -- so this needs no
            # special case at all: it is a region like any other, and the
            # backend's None is left for a caller that genuinely wants every
            # monitor at once.
            record_rect = self._to_absolute_rect(rect)

            self._armed_for_recording = True
            self._sync_bar_visibility()
            if self._on_recording_requested is not None:
                # `self.outcome` (== `self._chooser.after`) is "instant" or
                # "save" here -- ticket 9's `_land_recording` is what
                # actually acts on it, once the file is real; this branch
                # only ever hands the choice along.
                self._on_recording_requested(record_rect, self._delay, self.outcome)
            return
        self._hide_sensitive_text()
        if self.outcome == "instant":
            if setup_desktop.load_instant_saves():
                self._on_bar_save()
            else:
                self._on_bar_copy()

    # Logical pixels added around each hidden value. OCR's word boxes sit
    # tight on the glyphs, and the anti-aliased edges, descenders and
    # accents that fall just outside them are exactly the slivers that
    # make a covered value guessable.
    _HIDE_PADDING = 2.0

    # A found value already this much covered by an existing solid box is
    # not boxed again -- selecting a second region over the same text must
    # not stack a duplicate of every box, each needing its own erase.
    _HIDE_ALREADY_COVERED = 0.9

    # Two found boxes overlapping this much (of the smaller) are one value
    # found twice -- recognition reads small selections at two sizes, and
    # both reads usually find the same words -- and become one box.
    _HIDE_SAME_VALUE = 0.5

    def _hide_sensitive_text(self) -> None:
        """Black out sensitive text inside the current selection, when Hide
        sensitive is on.

        Reads the selection's pixels out of the frozen frame -- never the
        live screen, per CLAUDE.md's one rule -- asks the platform for the
        words in them, and puts a solid `Redact` over each value
        `sensitive.find_sensitive` flags -- including whatever the user
        added to their own hide list. The boxes are ordinary marks,
        added as one step of history: the user can erase any one of them,
        and a single undo takes the whole batch away.

        Coordinates: recognition happens on `Frame.crop`'s image, so every
        rect comes back in *cropped-image pixels*. Marks live in window
        coordinates. The mapping is the exact inverse of the one
        `shapes.render_selection` applies at export -- divide by that crop's
        own per-axis image-pixels-per-logical-unit ratio, then add the
        selection's origin -- which is what keeps a box over its word on a
        scaled display, where the two spaces differ by that ratio.
        """
        if self._selection is None or not self._chooser.hide_sensitive:
            return
        current = platform.current
        if not current.recognizes_text():
            return

        selection = QRectF(self._selection)
        cropped = self._frame.crop(selection.translated(self._frame.logical_origin))
        logical_width = cropped.logical_size.width()
        logical_height = cropped.logical_size.height()
        if cropped.image.isNull() or logical_width <= 0 or logical_height <= 0:
            return
        scale_x = cropped.image.width() / logical_width
        scale_y = cropped.image.height() / logical_height

        # Read per capture, not once at start-up: editing the list in
        # Settings takes effect on the next capture, with no restart.
        # Guarded because a capture must never fail over a settings file --
        # the worst a broken list may cost is the boxes it would have added.
        try:
            mine = sensitive.custom_list(setup_desktop.load_hide_list())
        except OSError:
            mine = sensitive.custom_list(None)

        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            lines = current.recognize_text(cropped.image)
        finally:
            QApplication.restoreOverrideCursor()

        existing = [
            QRectF(mark.start, mark.end).normalized()
            for mark in self._marks
            if isinstance(mark, Redact)
        ]
        pad = self._HIDE_PADDING
        found = [
            QRectF(
                selection.x() + image_rect.x() / scale_x - pad,
                selection.y() + image_rect.y() / scale_y - pad,
                image_rect.width() / scale_x + 2 * pad,
                image_rect.height() / scale_y + 2 * pad,
            )
            for image_rect in (finding.image_rect for finding in sensitive.find_sensitive(lines, mine))
        ]
        boxes = []
        for window_rect in self._merge_same_values(found):
            if self._already_covered(window_rect, existing):
                continue
            existing.append(window_rect)
            boxes.append(Redact(
                colour=QColor("#000000"),
                stroke_width=design.tokens.Metric.STROKE_DEFAULT,
                start=window_rect.topLeft(),
                end=window_rect.bottomRight(),
            ))

        self._mark_store.add_all(boxes)
        if boxes:
            count = len(boxes)
            self._show_toast("blur", f"Hid {count} item{'' if count == 1 else 's'}")

    @classmethod
    def _merge_same_values(cls, rects: list[QRectF]) -> list[QRectF]:
        """`rects` with every pair that is the same value found twice
        replaced by their union. The union, not either one: the two reads
        rarely agree to the pixel, and covering both is what guarantees
        neither leaves an edge of the value showing."""
        merged: list[QRectF] = []
        for rect in rects:
            rect = QRectF(rect)
            joined = True
            while joined:
                joined = False
                for other in merged:
                    overlap = rect.intersected(other)
                    smaller = min(rect.width() * rect.height(), other.width() * other.height())
                    if smaller > 0 and overlap.width() * overlap.height() >= smaller * cls._HIDE_SAME_VALUE:
                        merged.remove(other)
                        rect = rect.united(other)
                        joined = True
                        break
            merged.append(rect)
        return merged

    @classmethod
    def _already_covered(cls, rect: QRectF, covers: list[QRectF]) -> bool:
        area = rect.width() * rect.height()
        if area <= 0:
            return True
        for cover in covers:
            overlap = rect.intersected(cover)
            if overlap.width() * overlap.height() >= area * cls._HIDE_ALREADY_COVERED:
                return True
        return False

    def absolute_selection(self) -> QRectF | None:
        """The current selection in absolute virtual-desktop coordinates,
        or None if there isn't one.

        Read by `app.py` when a recording actually starts, rather than the
        rect handed over at commit time: the ready stage exists so the
        region can be reframed, and a recording that filmed the rectangle
        the user *first* dragged would make those handles a lie.
        """
        if self._selection is None:
            return None
        return self._to_absolute_rect(self._selection)

    def _confirm_window_pick(self, pos: QPointF) -> None:
        """Snap `_selection` to the window under `pos` (this widget's own
        window-local coordinates) and disarm picking. Only ever reached
        from `mousePressEvent` while `_picking_window` is armed.

        A miss leaves `_picking_window` armed and `_selection` as the
        last hover left it (already `None`, per `mouseMoveEvent`'s own
        miss handling below) -- the user can simply move and click again,
        rather than one mis-click ending the mode for good.
        """
        # No drag, so no anchor: the picked window's own rect is what
        # `_chrome_bounds` should resolve against.
        self._selection_anchor = None
        rect = self._geometry_provider.window_at(self._to_absolute(pos))
        if rect is None:
            return
        self._picking_window = False
        self._hovered_window = None
        self._commit_selection(self._to_local_rect(rect).toRect())

    def _select_full_screen(self) -> None:
        """Set `_selection` to the whole display the cursor is on, per
        the spec's "Full screen -- selection = the whole display" and
        this ticket's cursor-aware acceptance criterion. Snaps
        immediately -- no drag, no click needed past picking the row. Only
        on a desk with one monitor, or on Wayland where this window covers
        one; with more to choose between, `_enter_monitor_mode` arms
        instead (#53).

        The display is `_active_screen_rect`'s: the monitor under the last
        tracked move or, before any move has reached this window, the one
        the OS has the pointer on -- the monitor the chooser row was just
        picked from. It used to fall back to this window's own centre, which
        is the centre of the virtual desktop and names no monitor anyone is
        looking at. With a monitor mounted above two others that centre is
        the one point all three share, so `F` pressed on the second monitor
        captured the first, and put the toolbar there with it (#49).
        """
        # No drag, so no anchor: the picked display's own rect is what
        # `_chrome_bounds` should resolve against.
        self._selection_anchor = None
        rect = self._active_screen_rect()
        self._commit_selection(self._to_local_rect(rect).toRect())

    def _enter_monitor_mode(self) -> None:
        """Arm Full screen on a desk with more than one monitor: preview the
        monitor under the pointer and take the one clicked (#53).

        Window mode's loop, with "the monitor under the pointer" in place of
        "the window under the pointer" -- `mouseMoveEvent` previews and
        `_confirm_monitor_pick` takes. Snapping on the pick took whichever
        monitor the pointer happened to be on at that instant, with no way
        to change your mind: the chooser had stood down by then, so the
        other monitors could no longer be offered.

        The preview opens on `_active_screen_rect`, the monitor the chooser
        was just picked from, so something is highlighted before the pointer
        moves at all.
        """
        self._picking_monitor = True
        self.set_selection(None)
        start = self._active_screen_rect()
        self._hovered_monitor = start if start in self._monitor_geometries else None
        self._sync_chooser_visibility()
        self.update()

    def _confirm_monitor_pick(self, pos: QPointF) -> None:
        """Take the monitor under `pos` (window-local) and disarm. Only
        reached from `mousePressEvent` while `_picking_monitor` is armed.

        A press in a gap between monitors takes nothing and leaves the mode
        armed, as a click that misses every window does in Window mode.
        """
        monitor = self._monitor_containing(self._to_absolute(pos))
        if monitor is None:
            return
        self._picking_monitor = False
        self._hovered_monitor = None
        # No drag, so no anchor: the monitor's own rect is what
        # `_chrome_bounds` should resolve against.
        self._selection_anchor = None
        self._commit_selection(self._to_local_rect(monitor).toRect())

    def _leave_monitor_mode(self) -> None:
        """Escape while Full screen is armed: disarm, drop the preview and
        put the chooser back, having captured nothing.

        Back to the chooser rather than out of the snip, because what is on
        screen is a highlighted monitor waiting for a click -- what a hovered
        window is in Window mode, where Escape backs out a stage
        (`_handle_escape`). The next Escape leaves the snip.
        """
        self._picking_monitor = False
        self._hovered_monitor = None
        self._chooser.reopen()
        self._sync_chooser_visibility()
        self._apply_idle_cursor()
        self.update()

    def _select_browser_tab(self) -> None:
        """Set `_selection` to the page area of the frontmost browser.

        Snaps immediately, like `_select_full_screen` above and for the
        same reason: the rectangle is already decided, so there is nothing
        left to aim at.

        Uses the rect found when this overlay opened rather than asking
        again. By now the frozen frame is on screen and this window is in
        front of everything, so a second look would enumerate a desktop
        that no longer matches the pixels being captured -- and the answer
        could not have changed, because nothing can move while a frozen
        frame is up.

        Clipped to the frame, because a browser window can extend past the
        virtual desktop's edge (a maximised window's frame does, by the
        width of its invisible resize border) and the frame is the only
        source of pixels there is. Nothing found leaves the selection
        alone; the chooser will not normally offer the mode at all in that
        case.
        """
        # No drag, so no anchor -- the page's own monitor is what
        # `_chrome_bounds` should resolve against, as for Window and Full
        # screen above.
        self._selection_anchor = None
        if self._browser_tab is None:
            return
        _title, viewport = self._browser_tab
        self._commit_found_rect(viewport)

    def _select_active_window(self) -> None:
        """Set `_selection` to the frame of the window the user was in.

        `_select_browser_tab`'s shape: a rect learned while this window was
        hidden -- see `__init__` and `_finish_delayed_capture` for why no
        other moment will do -- committed at once. Nothing found leaves the
        selection alone, and the chooser greys the row in that case anyway.
        """
        self._selection_anchor = None
        if self._active_window is None:
            return
        _title, rect = self._active_window
        self._commit_found_rect(rect)

    def _commit_found_rect(self, absolute: QRectF) -> None:
        """Commit an absolute logical rect a provider found, clipped to the
        frame -- never to one monitor, so a window straddling two displays
        comes out whole. A rect entirely off the frame commits nothing.
        """
        usable = QRectF(absolute).intersected(
            QRectF(self._frame.logical_origin, self._frame.logical_size)
        )
        if usable.isEmpty():
            return
        self._commit_selection(self._to_local_rect(usable).toRect())

    def _preselect_last_region(self) -> None:
        """Open Region mode on the rectangle the last snip was taken from,
        when `setup_desktop.load_reuse_last_region()` asks for it.

        A *pre*-selection, deliberately: `set_selection`, never
        `_commit_selection`. Committing is what "the user has chosen
        something" means -- it is the moment `instant` finishes the snip and
        the moment a recording arms -- and neither should happen because an
        overlay opened. What the user gets is the rectangle already framed
        with the toolbar on it: press Copy or Save to take it, drag a new
        box anywhere outside it to frame something else instead (which
        `mousePressEvent` already supports), or nudge an edge to adjust it.
        Nothing is captured until they say so.

        Stills only. On the record side the equivalent would have to commit
        -- that is what puts the record bar up and arms the region -- and
        arming a recording as a side effect of opening the overlay is not
        something anyone asked for.

        The stored rectangle is absolute, and the desktop it was stored
        against may not be the desktop it is being recalled onto -- a
        monitor unplugged, a laptop undocked, screens rearranged. So it is
        clipped to the frame's own span before use: the frame is the only
        source of pixels there is, and a selection reaching past it would
        crop from nothing and hand back black. It must also still touch a
        real monitor, because the frame's span is the *union* of the
        monitors and a staggered desk leaves gaps inside that union which
        no display shows. A rectangle that survives neither test leaves the
        overlay exactly as it would have been with the preference off --
        an ordinary empty Region drag, which is the right thing to fall
        back to and needs no explaining to the user.

        On Wayland with more than one monitor this reaches only the
        interactive one. A client there cannot span two outputs with a
        single surface, so `open_overlay` hands this window a frame cropped
        to that monitor and that monitor alone as `_monitor_geometries`; a
        rectangle remembered on any other output then fails the
        touches-a-real-monitor test above and is dropped, leaving an
        ordinary empty drag. That is the correct outcome rather than a gap
        to close -- the other monitor's pixels are not in this frame to
        crop from. X11 and Windows both span the whole virtual desktop in
        one window and have no such limit.

        Called when the overlay opens and when Region is re-dispatched,
        never straight off the toggle: flipping the switch on would
        otherwise pre-select immediately, and pre-selecting stands the
        chooser down -- so the row would vanish from under the pointer that
        just clicked it. The preference takes effect on the next overlay,
        which is also the only place the word "opens" can mean anything.
        """
        if self._chooser.kind == "record":
            return
        # Read off the toggle, not straight from config: the two agree at
        # construction (it is seeded from the same value), and within a
        # session the control the user can actually see is the authority.
        if not self._chooser.reuse_last_region:
            return
        stored = setup_desktop.load_last_region()
        if stored is None:
            return
        absolute = QRectF(*stored)
        usable = absolute.intersected(
            QRectF(self._frame.logical_origin, self._frame.logical_size)
        )
        if usable.isEmpty():
            return
        if not any(usable.intersects(geometry) for geometry in self._monitor_geometries):
            return
        # No drag, so no anchor -- the recalled rectangle's own monitor is
        # what `_chrome_bounds` should resolve against, exactly as for
        # Window and Full screen above.
        self._selection_anchor = None
        # `usable` is absolute; `_selection` is window-local.
        self.set_selection(self._to_local_rect(usable).toRect())
        # Set *after* `set_selection`, which clears it: this is the one
        # caller for which the flag must survive.
        self._recalled_selection = True
        self._hide_sensitive_text()

    def _monitor_at(self, absolute_point: QPointF) -> QRectF:
        """The `_monitor_geometries` entry containing `absolute_point`
        (absolute logical virtual-desktop coordinates), or the frame's
        own full span if none does -- a point can land outside every
        known monitor only when `_monitor_geometries` wasn't supplied
        accurately, and the whole capture is the only sane rect left to
        offer rather than raising.
        """
        found = self._monitor_containing(absolute_point)
        if found is not None:
            return found
        return QRectF(self._frame.logical_origin, self._frame.logical_size)

    def _monitor_containing(self, absolute_point: QPointF) -> QRectF | None:
        """The `_monitor_geometries` entry containing `absolute_point`
        (absolute logical), or None when it is on no monitor -- in a gap of
        a staggered desk, or off the desk entirely.
        """
        for geometry in self._monitor_geometries:
            if geometry.contains(absolute_point):
                return geometry
        return None

    def _to_absolute(self, local_point: QPointF) -> QPointF:
        """This widget's own window-local logical point -> absolute
        logical virtual-desktop point -- the space `GeometryProvider`/
        `_monitor_geometries` both use, the same conversion `Overlay.
        _to_absolute` performs for its own (differently-anchored) local
        space.
        """
        return local_point + self._frame.logical_origin

    def _to_local_rect(self, absolute_rect: QRectF) -> QRectF:
        """Absolute logical virtual-desktop rect -> this widget's own
        window-local logical rect -- the inverse of `_to_absolute`,
        applied to a rect rather than a point for `_confirm_window_pick`/
        `_select_full_screen`, whose `GeometryProvider`/
        `_monitor_geometries` results both arrive in absolute coordinates.
        """
        return QRectF(absolute_rect.topLeft() - self._frame.logical_origin, absolute_rect.size())

    def _to_absolute_rect(self, rect: QRectF) -> QRectF:
        """This widget's own window-local logical rect -> absolute logical
        virtual-desktop rect -- the inverse of `_to_local_rect`, and the
        one call site is `_commit_selection`'s record branch: the recorder
        (SNX-122) needs the same absolute space `RecorderRegistry.start`
        expects, and `logical_origin` can be negative (a monitor left of or
        above the primary), so this must be a real translate rather than an
        `abs()`.
        """
        return QRectF(rect).translated(self._frame.logical_origin)

    def _chrome_bounds(self) -> QRectF:
        """The rect every piece of floating chrome -- bar, popovers, menus
        -- must stay inside, in this window's own local coordinates.

        This is the monitor the current selection sits on, **not**
        `self.rect()`. On X11 this one window spans the whole virtual
        desktop, so its own rect is the union of every monitor, and a
        union is not a place chrome can safely be put: monitors of
        different heights, or mounted at different vertical offsets, leave
        gaps inside that union which no monitor displays. Chrome clamped
        to the union lands in one of those gaps and is invisible even
        though it is, technically, inside the window.

        The monitor is where the selection's drag *started*
        (`_selection_anchor`) whenever that is known. "The monitor I ran the
        selection on" is the whole of what a user means here, and it is the
        one answer that cannot surprise them: a drag begun on the left
        monitor and carried a little way past the bezel would otherwise hand
        its toolbar to the middle monitor the instant a few more pixels of
        the rectangle landed there, moving the controls away from the screen
        being worked on for no reason the user can see.

        Largest overlap with the selection is the fallback, for selections
        that never came from a drag at all -- Window and Full screen pick a
        rect outright -- and it beats "whichever monitor holds the centre"
        for those, since a rect can perfectly well have its centre in a gap.
        With no selection it is the monitor being worked on
        (`_active_screen_rect`), the one the chooser row is on. Never the
        window's centre: that is the virtual desktop's centre, and with a
        monitor mounted above two others it is the single point all three
        share, which put the close button -- and Full screen's whole capture
        -- on the first monitor listed wherever the user was (#49). A
        selection that overlaps no monitor (it lies entirely inside a gap)
        falls back to `_monitor_at`, whose own last resort is the frame's
        full span.

        That monitor is then inset by whatever the desktop's own chrome
        reserves on it (`_usable_area`). The bar, its menus and tool hint,
        the popovers and the toast are all clamped into this rect, and a
        dock paints over this window: before the inset, a selection reaching
        the bottom of the monitor put the whole bar under a bottom dock,
        where none of it could be clicked.
        """
        return self._to_local_rect(self._usable_area(self._chrome_monitor()))

    def _chrome_monitor(self) -> QRectF:
        """The monitor `_chrome_bounds` is carved from, in absolute
        coordinates, chosen by the rules that method's docstring gives.
        """
        if self._selection is not None:
            selection = QRectF(self._selection)
            if self._selection_anchor is not None:
                anchor = self._to_absolute(self._selection_anchor)
                for geometry in self._monitor_geometries:
                    if geometry.contains(anchor):
                        return geometry
            best: QRectF | None = None
            best_area = 0.0
            for geometry in self._monitor_geometries:
                overlap = self._to_local_rect(geometry).intersected(selection)
                area = overlap.width() * overlap.height()
                if area > best_area:
                    best, best_area = geometry, area
            if best is not None:
                return best
            return self._monitor_at(self._to_absolute(selection.center()))
        return self._active_screen_rect()

    def _on_delay_changed(self, delay: str) -> None:
        """One delay, set from either surface: the chooser row or the
        floating bar's popover. `_delay` is what the capture reads, so both
        surfaces feed it and are seeded back from it -- the way the mode is
        shared between the chooser and the bar's chip.

        The early return is what ends the round trip: seeding the chooser
        emits `delayChanged` straight back here with the value just stored.
        """
        if delay == self._delay:
            return
        self._delay = delay
        self._chooser.set_delay(delay)
        self._popover.set_delay(delay)

    @property
    def outcome(self) -> str:
        """What should happen to this snip once it is taken.

        Always a value, never None: the chooser is seeded from Settings at
        launch, so "the user did not say" and "the user chose what Settings
        already said" are the same answer -- and the per-snip control is
        allowed to differ without writing back, per the handoff.
        """
        return self._chooser.after

    def _sync_chooser_visibility(self) -> None:
        """The chooser is up whenever there is nothing selected yet.

        That is the whole of its rule: it answers "what am I capturing",
        which stops being a question the moment something is. It never
        shares the screen with the floating bar, which has the opposite
        condition, so the two may safely share a widget stack.

        Unlike the placeholder this replaces, it does not vanish when a
        picking mode is armed -- it collapses to a 26px tab against the same
        edge, which is the handoff's answer to "how does it stay out of the
        way of a mode that needs the whole screen". See `chooser.Chooser`.
        """
        if self._selection is not None or not self.isVisible():
            self._chooser.hide_all()
            return
        screen_rect = self._active_screen_rect()
        # The chooser hangs from the top edge, so it is the surface the
        # desktop's own bar hides -- give it the part of the monitor it can
        # actually use.
        # Converted by the frame's origin, like every other absolute rect
        # this window places, rather than by `geometry().topLeft()`. The two
        # agree only while the window is exactly where it was put, and on
        # Wayland a client is never told where its window is -- so a primary
        # monitor anywhere but the desktop's origin put the row off it.
        self._chooser.set_screen(
            self._usable_area(screen_rect),
            QPoint(
                round(self._frame.logical_origin.x()),
                round(self._frame.logical_origin.y()),
            ),
        )

    def _active_screen_rect(self) -> QRectF:
        """The monitor the snip opened on, in absolute coordinates.

        The handoff is emphatic that everything here positions against a
        monitor and never the virtual desktop: on a staggered multi-monitor
        setup the desktop's centre is a gap between screens. `screenAt` is
        the pointer's own monitor -- the one being looked at -- with the
        primary as the fallback the handoff names.

        `_cursor_pos` (tracked from real move events) is preferred over
        `QCursor.pos()`, per this file's standing rule against reaching for
        global cursor state: it is what lets the row follow the pointer
        across a bezel (`_follow_pointer_to_its_monitor`) without a test
        having to drive a system-wide cursor. The global position remains
        the fallback for the one moment nothing has been tracked yet --
        the overlay opening, before any move has happened.

        Both are resolved against `_monitor_geometries`, never answered with
        a `QScreen`'s own geometry: this window covers only those monitors
        -- on Wayland with several, just the interactive one -- and a
        monitor it does not cover is nowhere its chrome can be drawn. A
        pointer on none of them gets the primary, as `_interactive_geometry`
        already decides it.

        This is the one answer to "which monitor" while nothing is selected.
        The close button, a toast, the delay countdown and Full screen's own
        pick all take it through `_chrome_monitor`, so none of them can land
        on a different monitor from the row (#49).
        """
        if self._cursor_pos is not None:
            return self._monitor_at(self._to_absolute(self._cursor_pos))
        pointer = self._monitor_containing(QPointF(QCursor.pos()))
        if pointer is not None:
            return pointer
        return _interactive_geometry(self._monitor_geometries)

    def _follow_pointer_to_its_monitor(self) -> None:
        """Move the chooser row to the monitor the pointer is now on.

        The row is placed against one monitor, and which one was decided
        when the overlay opened -- so pressing the shortcut while working
        on one screen and then crossing to another to frame something left
        every control back where you started. On a three-monitor desk that
        is a round trip per snip, and it reads as the controls appearing on
        the wrong screen: reported as "the controls pop up on the monitor
        im focused on, not the monitor i started capturing on".

        Only while the row is actually up. Once something is selected the
        chooser has stood down and the floating bar takes over, and that
        one is anchored to where the drag *started* on purpose
        (`_chrome_bounds`) -- chrome that chased the pointer mid-drag would
        be worse than chrome that stayed put.

        Guarded on the monitor actually changing, because this runs from
        `mouseMoveEvent`: re-laying the row out on every pixel of every
        move would repaint it across the frozen frame for no visible
        change.
        """
        if self._cursor_pos is None or self._selection is not None:
            return
        if not self.isVisible():
            return
        monitor = self._monitor_at(self._to_absolute(self._cursor_pos))
        if monitor == self._chooser_monitor:
            return
        self._chooser_monitor = monitor
        self._sync_chooser_visibility()
        # The close button answers the same question the row does
        # (`_chrome_monitor`), so it crosses with it.
        self._reposition_close_button()

    def _sync_bar_destination(self) -> None:
        """Put the chooser's destination on the split button's face.

        "The chooser sets the split button's face" -- so the choice made
        before the snip is already under the cursor when the bar appears,
        and the caret is only for changing your mind. `instant` means the
        clipboard, `review` the review window, and `save` writes a file.

        `edit` is the odd one and is mapped deliberately rather than left
        to fall through a default. It is not a destination at all -- it is
        "put the toolbar up and let me decide" -- so the face has to show
        *something*, and Copy is both the most-used ending and the one the
        caret is cheapest to change away from. Left as a `.get` default
        this looked like an oversight; it is a choice.
        """
        face = {
            "instant": "Copy",
            "edit": "Copy",
            "save": "Save",
            "review": "Open",
        }.get(self._chooser.after, "Copy")
        self._bar.set_destination(face)

    def _on_chooser_mode(self, mode: str) -> None:
        """A mode armed from the chooser. One piece of state, two surfaces:
        the bar's own chip is seeded from the same value.
        """
        self._on_capture_mode_selected(mode)

    def _on_chooser_immediate(self, mode: str) -> None:
        """A mode with nothing left to aim at fires the grab rather than
        arming: Browser, and Full screen on a desk with one monitor
        (`Chooser._fires_immediately`).
        """
        self._on_capture_mode_selected(mode)

    def _sync_bar_visibility(self) -> None:
        """Show/hide and reposition the floating bar to match `_selection`.

        Guarded on `self.isVisible()` -- not just "is there a selection" --
        so this window's own many pixel-sampling tests, none of which ever
        call `.show()`, never end up with the bar painted into a `grab()`
        they didn't ask for: a child widget's own `.show()` call is enough
        to make Qt paint it in a `grab()` regardless of whether this window
        itself was ever shown, so visibility has to be gated here rather
        than unconditionally following `_selection`.
        """
        # The annotation bar belongs to stills. Gated on the *kind* rather
        # than on `_armed_for_recording`, because the bar follows the
        # selection and a selection exists from the first pixel of a drag
        # -- so gating on "armed" showed the whole screenshot toolbar for
        # the length of every recording drag and only hid it on release.
        # Reported twice as "i shouldnt see the whole screenshooting tools".
        if self._armed_for_recording or self._chooser.kind == "record":
            self._bar.hide()
            self._style_popover.hide()
            self._popover.hide()
            self._tool_hint.hide()
            for menu in self._family_menus.values():
                menu.hide()
        elif self._selection is not None and self.isVisible():
            self._sync_bar_destination()
            self._arm_default_tool()
            self._bar.reposition(self._selection, self._chrome_bounds())
            self._bar.show()
            self._sync_tool_hint()
        else:
            self._bar.hide()
            self._style_popover.hide()
            self._popover.hide()
            self._tool_hint.hide()
            for menu in self._family_menus.values():
                menu.hide()

    def _preview_tool(self, tool: str) -> None:
        """Name the tool under the cursor without arming it. Reverts on
        leave, so hovering only ever reads. Not while the style popover is
        open: the strip gives way to it (`_sync_tool_hint`).
        """
        if (
            not self.isVisible()
            or tool not in design.tokens.TOOL_HINTS
            or not self._style_popover.isHidden()
        ):
            return
        self._tool_hint.set_tool(tool)
        self._tool_hint.show()
        self._tool_hint.raise_()
        self._reposition_tool_hint()

    def _arm_default_tool(self) -> None:
        """Arm the pen the first time the toolbar comes up for a snip.

        The bar is the annotate-in-place surface, and it used to appear
        with nothing armed at all -- so the first stroke of every
        annotation cost a trip to the bar to pick the tool that was going
        to be picked anyway. Pen is the one that is: it is
        `tokens.TOOLS`' own first entry.

        Once only, and only while nothing is armed. `_sync_bar_visibility`
        runs on every mouse-move of a live drag, so re-arming here
        unconditionally would drag whatever tool the user had actually
        chosen back to pen underneath them. Anything the user has already
        selected therefore wins over this.

        The eraser is checked separately because it is the one tool that
        is *not* an `active_tool`: `set_eraser_active` arms it through its
        own flag, leaving `active_tool` at None. Reading only `active_tool`
        would see "nothing armed", arm the pen over an eraser the caller
        had just switched on, and take the pointer cursor with it.

        The trade this makes, deliberately: a press inside the selection
        now draws where it used to do nothing. That is what "the pen is
        armed" has always meant for a tool picked by hand, and the
        selection can still be reframed by its handles or replaced by a
        drag outside it.
        """
        if self._recalled_selection:
            # Nothing is armed over a region the user did not ask for --
            # and the latch is deliberately not spent, so the pen still
            # arms for whatever selection they do make. Arming here was the
            # other half of "it comes up on every screenshot and i dont
            # want it to": on a recalled region the size of a monitor,
            # every press landed inside it and drew.
            return
        if self._armed_default_tool:
            return
        self._armed_default_tool = True
        if self._bar.active_tool is None and not self._eraser_active:
            self._bar.select_tool(design.tokens.TOOLS[0])

    def _sync_tool_hint(self) -> None:
        """Name the active tool under the bar, and say what it does, while
        the bar is up and the style popover is not.

        The trays used to come up by themselves for every tool that had
        one, so the bar and the tray under it stood about 110px tall. The
        strip holds their place, so the active tool is still named on screen
        with what it does -- the one part of a tray that was always worth
        having up.

        It gives way to the style popover rather than sharing the screen
        with it. Where the bar has no room below it both would open above
        it, one on top of the other, and the lit slot already says which
        tool the popover is styling.

        Gated on the bar's own visibility rather than re-checking
        `_selection`/`self.isVisible()` directly -- the bar is already the
        single source of truth for "is this window's chrome allowed to be on
        screen right now", and everything here hangs off it.
        """
        tool = self._bar.active_tool
        if not (self._bar.isVisible() and tool and self._style_popover.isHidden()):
            self._tool_hint.hide()
            return
        # Told the tool each time, so it never names whichever it showed last.
        self._tool_hint.set_tool(tool)
        self._tool_hint.show()
        self._tool_hint.raise_()
        self._reposition_tool_hint()

    def _reserved_margins(self, monitor: QRectF) -> QMargins:
        """Logical pixels along each edge of `monitor` that the desktop's
        own chrome owns -- see `platform.Platform.reserved_margins`.
        `monitor` is absolute, the space `_monitor_geometries` is in.

        Everything this window draws against a monitor's edges has to clear
        them: GNOME paints its top bar and its dock over an always-on-top
        window, so a chooser hung flush from the top edge is behind the bar,
        and a floating bar clamped to the bottom edge is behind the dock.
        """
        key = (round(monitor.x()), round(monitor.y()))
        if key not in self._reserved_margins_cache:
            screen = QGuiApplication.screenAt(monitor.center().toPoint())
            self._reserved_margins_cache[key] = (
                platform.current.reserved_margins(screen)
                if screen is not None
                else QMargins()
            )
        return self._reserved_margins_cache[key]

    def _usable_area(self, monitor: QRectF) -> QRectF:
        """`monitor` minus what the desktop's own chrome reserves on it: the
        part of that monitor chrome can be drawn on and still be clicked.
        Absolute in, absolute out.
        """
        margins = self._reserved_margins(monitor)
        return monitor.marginsRemoved(
            QMarginsF(margins.left(), margins.top(), margins.right(), margins.bottom())
        )

    def _reposition_close_button(self) -> None:
        """Put the close button in the top-right corner of `_chrome_bounds`
        -- the monitor the selection is on or, before there is one, the
        monitor the chooser row is on.

        SNX-80 put it in the top-right corner of the *window*, which is the
        top-right corner of the whole virtual desktop once one window spans
        every monitor. On a desktop whose rightmost monitor is mounted
        lower than the tallest one, that corner is in the gap above it: the
        button was drawn 170px above the only screen that could have shown
        it, so the one affordance this ticket exists to provide -- "a
        visible way to cancel a snip" -- was invisible on exactly the
        multi-monitor setups it matters most on. Esc still worked, which is
        why nothing caught it.
        """
        # `_chrome_bounds` already stops short of whatever the desktop
        # reserves along the monitor's top and right edges.
        bounds = self._chrome_bounds()
        self._close_button.move(
            round(bounds.right() - self._CLOSE_BUTTON_MARGIN - _CloseButton._SIZE),
            round(bounds.top() + self._CLOSE_BUTTON_MARGIN),
        )

    def _reposition_tool_hint(self) -> None:
        """Centre the tool hint strip under the bar, `TRAY_OFFSET_Y` below
        it -- where the old tray spec put its tray: "Sits 8px below the bar,
        centred on it."

        Clamped into `_chrome_bounds` afterwards, the same monitor rect the
        bar itself is clamped to: the bar can legitimately sit close enough
        to its monitor's bottom edge that a strip placed 8px below it would
        hang off that monitor -- on a multi-monitor desktop that means a gap
        displaying nothing, not merely a screen edge. When there is no room
        below, the strip flips above the bar rather than being pushed back
        over it.
        """
        strip = self._tool_hint
        metric = design.tokens.Metric
        bar_geometry = self._bar.geometry()
        bounds = self._chrome_bounds()
        size = strip.sizeHint()
        center_x = bar_geometry.center().x()
        top = bar_geometry.bottom() + metric.TRAY_OFFSET_Y
        if top + size.height() > bounds.bottom():
            top = bar_geometry.top() - metric.TRAY_OFFSET_Y - size.height()
        top = max(bounds.top(), min(top, bounds.bottom() - size.height()))
        left = center_x - size.width() / 2
        left = max(bounds.left(), min(left, bounds.right() - size.width()))
        strip.setGeometry(round(left), round(top), size.width(), size.height())

    def _sync_bar_undo_redo(self) -> None:
        self._bar.set_undo_enabled(self.can_undo)
        self._bar.set_redo_enabled(self.can_redo)

    # -- toast (SNX-45) ------------------------------------------------------

    def _show_toast(self, icon_name: str, text: str) -> None:
        """Show `_toast` for `icon_name`/`text`, gated on `self.isVisible()`.

        Mirrors `_sync_bar_visibility`'s own guard on `_bar`: a child
        widget's `.show()` call is enough to make Qt paint it into a
        `grab()` of this window regardless of whether this window itself
        was ever shown, and none of this file's many other pixel-sampling
        tests call `.show()` first -- so a toast triggered by copy()/
        save()/clear()/discard() must stay off screen until this window
        actually is, same as the bar and popovers already do.
        """
        if self.isVisible():
            self._toast.show_message(icon_name, text, self._chrome_bounds())

    # -- top hint HUD (SNX-46) -----------------------------------------------

    @property
    def hints_enabled(self) -> bool:
        return self._hints_enabled

    def set_hints_enabled(self, enabled: bool) -> None:
        """Toggle the top hint HUD -- the preference docs/design/overlay-
        redesign.md's "Top hint HUD" section puts it behind (`hints`,
        default off as of SNX-65). Turning it off hides `_hud` immediately;
        turning it back on shows it again as soon as `_sync_hud_visibility`'s
        other gate -- `self.isVisible()` -- is also true, same as flipping
        `_selection` does for `_bar`.
        """
        self._hints_enabled = enabled
        self._sync_hud_visibility()

    def _sync_hud_visibility(self) -> None:
        """Show/hide `_hud` to match `_hints_enabled` and this window's own
        visibility.

        Gated on `self.isVisible()` for the same reason `_sync_bar_
        visibility`/`_show_toast` are: a child widget's own `.show()` call
        is enough to make Qt paint it into a `grab()` of this window
        regardless of whether this window itself was ever shown, and this
        file's many pixel-sampling tests never call `.show()` first -- so
        the HUD must stay off screen until this window actually is, same as
        the bar/popovers/toast already do, on top of respecting the preference.
        """
        if self._hints_enabled and self.isVisible():
            self._hud.show()
            self._hud.raise_()
        else:
            self._hud.hide()

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
        self._mark_store.add(shape)

    @property
    def _marks(self) -> tuple[Shape, ...]:
        """Read-only view of the store, so this file's painting and
        hit-testing sites read exactly as they did before the model moved
        out. Every mutation goes through `_mark_store`.
        """
        return self._mark_store.marks

    def _on_marks_changed(self) -> None:
        """The single place anything reacts to the ink layer changing --
        the bar's undo/redo buttons and a repaint, once, however the change
        arrived.
        """
        self._sync_bar_undo_redo()
        self.update()

    @property
    def marks(self) -> tuple[Shape, ...]:
        """Ink layer contents, in paint order. A copy, not the live list,
        mirroring `Canvas.shapes` in editor.py."""
        return self._mark_store.marks

    # -- undo / redo / clear (SNX-39; SNX-70 folds the eraser in, SNX-72 the
    # -- clear button) ------------------------------------------------------
    # Two stacks of `_MarkAction`s, per docs/design/overlay-redesign.md's
    # "Undo / redo": undo pops the newest action off `_undo` and inverts it
    # onto `_redo`; redo pops it back and replays it onto `_undo`. Unlike
    # the plain end-of-list push/pop this used to be before SNX-70, each
    # action carries its own `index` -- needed now that an 'erase' can
    # remove from the middle of `_marks`, not just the end the way an 'add'
    # always does -- so undo/redo restore exactly the draw-order position
    # the action happened at either way. `index` is unused for a 'clear'
    # (SNX-72), which always empties/restores the whole list rather than
    # one position in it.

    @property
    def can_undo(self) -> bool:
        return self._mark_store.can_undo

    @property
    def can_redo(self) -> bool:
        return self._mark_store.can_redo

    def undo(self) -> None:
        """Invert the newest action on the undo stack and move it to the
        redo stack.

        An 'add' action (`add_mark` appending a mark) is undone by removing
        that mark from `_marks`; an 'erase' action (SNX-70: `erase_at`
        removing one) is undone by reinserting it -- both read
        `_MarkAction.index`, which is what puts an undone erase back at
        exactly the draw-order position it was removed from rather than at
        the end. A 'clear' action (SNX-72: `clear()` emptying the whole
        list) is undone by restoring every mark it carries, in the same
        draw order they were in before the clear. A no-op with nothing to
        undo.
        """
        self._mark_store.undo()

    def redo(self) -> None:
        """Replay the newest action on the redo stack and move it back to
        the undo stack.

        Mirrors `undo()`: an 'add' action is redone by reinserting the mark
        at `index`; an 'erase' action is redone by removing it again from
        that same position; a 'clear' action is redone by emptying
        `_marks` again.

        A no-op with nothing to redo -- either nothing has been undone yet,
        or a mark committed since (see `add_mark`) already cleared the
        stack.
        """
        self._mark_store.redo()

    def clear(self) -> None:
        """Move every mark to the undo stack as a single step, and toast
        `Ink cleared`.

        SNX-72: clear used to drop `_marks` and both stacks outright via
        `_empty_marks` (per the spec's "Clear-ink empties both and
        toasts"), leaving no way back -- but clear sits in the same bar as
        undo/redo, right next to redo, and is the single most destructive
        button in the tool. It now takes its turn in the general undo/redo
        history instead, the same way SNX-70 folded the eraser in: the
        whole mark list is recorded as one `_MarkAction` (kind `'clear'`)
        and `_marks` emptied, so `undo()` restores every mark in its
        original draw order, `redo()` re-clears, and a mark committed after
        undoing a clear (via `add_mark`) drops it from `_redo` like any
        other action. Clearing with nothing on screen is a no-op -- it
        does not push an empty step, matching `undo`/`redo`'s own
        "nothing to do" contract.
        """
        if self._mark_store.clear():
            self._show_toast("trash", "Ink cleared")

    def discard(self) -> None:
        """Discard every mark and both stacks outright, and toast
        `Ink discarded`.

        Per the spec's keyboard table, "Esc -- discard all ink, toast Ink
        discarded" -- its own method and toast message, since Esc's
        wording is deliberately distinct from the floating bar's own
        clear-ink button. Unlike `clear()` (SNX-72), this is *not* folded
        into the undo/redo history: Esc is a leave-immediately gesture,
        not a bar button living next to undo/redo where a mis-aimed click
        is easy, so there is no ticket asking for a way back from it.
        Called by `_handle_escape` (SNX-47) as the first stage of Esc's
        two-stage behaviour, whenever there is ink present to discard.
        """
        self._empty_marks()
        self._show_toast("trash", "Ink discarded")

    def _empty_marks(self) -> None:
        """`discard()`'s body: empty `_marks` and both the undo and redo
        stacks, and resync the bar's undo/redo buttons, without deciding
        which toast (if any) to show -- that choice is the caller's own.
        """
        self._mark_store.reset()

    def _cancel(self) -> None:
        """The close button's own handler (SNX-80): discard any ink and
        close, unconditionally, in the single click a visible button gets.

        Unlike `_handle_escape`'s two-stage discard-then-close -- which
        exists so a first press can back a mid-annotation user out of their
        ink without losing the overlay itself -- a click on a control whose
        whole point is "leave now" doesn't get to ask for a second one.
        `_empty_marks()` alone, not `discard()`: `discard()`'s own "Ink
        discarded" toast would never actually be seen, since this window is
        about to close and `hideEvent` hides `_toast` along with everything
        else, so showing it here would just be dead code with extra steps.
        """
        self._empty_marks()
        self.close()

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

        SNX-70: the removed mark is pushed onto the general undo stack
        (`_undo`) as an 'erase' `_MarkAction`, clearing `_redo` the same
        way `add_mark` does -- so an erase takes its turn in the same
        undo/redo history as any other action, and Ctrl+Z/the bar's Undo
        button (already wired to `undo()`) actually restore it, instead of
        the private single-slot `undo_erase` this used to feed that nothing
        in the UI ever called.
        """
        return self._mark_store.erase(point)

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
        """Flatten the marks present *right now* onto the selection's crop,
        place the result on the clipboard, and toast `Copied to clipboard`.
        """
        from snipux.app import copy_image_to_clipboard

        image = self.rendered_image()
        copy_image_to_clipboard(image)
        self._show_toast("copy", "Copied to clipboard")
        self._report_capture(image, None)

    # Subdirectory of ~/Pictures saves land in -- per the spec's "Save
    # writes a timestamped PNG to ~/Pictures/snipux, creating the
    # directory." `app.save_image` defaults to a bare ~/Pictures for its
    # own other callers, so the subdirectory is supplied here rather than
    # changed there.
    SAVE_SUBDIRECTORY = "snipux"

    def save(self) -> Path:
        """Flatten the marks present *right now* onto the selection's crop
        and write it as a timestamped PNG under ~/Pictures/snipux, creating
        that directory if it doesn't exist yet. Returns the path written,
        and toasts `Saved to ~/Pictures/snipux`.
        """
        from snipux.app import save_image

        directory = Path.home() / "Pictures" / self.SAVE_SUBDIRECTORY
        image = self.rendered_image()
        path = save_image(image, directory)
        self._show_toast("save", f"Saved to ~/Pictures/{self.SAVE_SUBDIRECTORY}")
        self._report_capture(image, path)
        return path

    def _report_capture(self, image: QImage, path: "Path | None") -> None:
        """Tell the caller a snip actually happened, and what came of it.

        Separate from `on_dismissed`, which fires for every way the overlay
        ends -- Esc included. A cancelled snip is not a capture, and must
        not open a review window.
        """
        self._remember_last_region()
        if self._on_captured is not None:
            self._on_captured(image, path)

    def _remember_last_region(self) -> None:
        """Write down the rectangle this snip came from, so the chooser's
        `Last region` mode can offer it back.

        Recorded here -- the one moment a capture is known to have really
        happened -- rather than when a selection is committed. A rectangle
        dragged, reconsidered and abandoned with Esc is not what anyone
        means by "the last region", and every mode funnels through
        `_report_capture` on its way out, so this needs no per-mode wiring.
        """
        if self._selection is None:
            return
        absolute = self._to_absolute_rect(QRectF(self._selection)).toRect()
        if absolute.width() <= 0 or absolute.height() <= 0:
            return
        setup_desktop.save_last_region(
            (absolute.x(), absolute.y(), absolute.width(), absolute.height())
        )

    # SNX-62: the bar's Copy/Save buttons, unlike `copy()`/`save()`
    # themselves, must also end the snip -- taking a snip should end the
    # snip, the same as Enter's own copy-and-dismiss below already does.
    # Wired to these wrapper methods rather than closing inside `copy()`/
    # `save()` directly: those two stay pure flatten-and-emit actions,
    # callable (and tested) without a close side effect, e.g. from Enter's
    # own handler, which already pairs its own `self.close()` explicitly,
    # or from a test asserting on `_toast` after the call -- `hideEvent`
    # hides `_toast` along with everything else, so a `close()` buried
    # inside `copy()`/`save()` would make that toast invisible again before
    # a caller ever got to look at it.

    def _on_bar_copy(self) -> None:
        """The floating bar's Copy button: copy, then dismiss."""
        self.copy()
        self.close()

    def _on_bar_save(self) -> None:
        """The floating bar's Save button: save, then dismiss."""
        self.save()
        self.close()

    def _on_bar_open(self) -> None:
        """Open: save the snip, then hand it to the review window.

        `app.py`'s `_on_captured` is what actually opens that window, and
        it asks this overlay's `outcome` -- so choosing Open here is the
        same thing as having chosen Review in the chooser, decided one
        stage later. Saving first rather than copying is deliberate: the
        review window edits a file, and the handoff's own note for this
        destination is "Review window -- annotate, crop, export".
        """
        self._chooser.set_after("review")
        self.save()
        self.close()

    def _open_destination_menu(self) -> None:
        """The split action's caret: the two destinations its face is not.

        A top-level popup, for the reason `FlowMenu`'s own docstring gives
        -- it has to paint above the hint pill below the bar, and a parent
        carrying an effect would trap it.
        """
        current = self._bar.destination()
        rows = [
            (name, name, note, key, "")
            for name, note, key in (
                ("Copy", "Image on the clipboard, paste anywhere.", "C"),
                ("Save", "Straight to your snips folder.", "S"),
                ("Open", "Review window -- annotate, crop, export.", "O"),
            )
        ]
        menu = FlowMenu(rows, current, design.tokens.FlowMetric.MENU_W_DEST, None)
        menu.chosen.connect(self._on_destination_chosen)
        anchor = self._bar._action
        top_left = anchor.mapToGlobal(anchor.rect().topLeft())
        menu.open_below(QRect(top_left, anchor.size()))
        self._destination_menu = menu

    # The caret's three destinations, as `tokens.AFTER_CAPTURE` values --
    # the inverse of the mapping `_sync_bar_destination` uses to put a face
    # on the button in the first place.
    #
    # Copy maps to `edit`, not `instant`. `instant` means "finish the snip
    # the moment a selection is committed", a stage this snip is already
    # past by the time a bar exists to press; `edit` is the value whose
    # face is Copy and whose meaning is "the bar decides", which is exactly
    # what just happened.
    _DESTINATION_OUTCOMES = {"Copy": "edit", "Save": "save", "Open": "review"}

    def _on_destination_chosen(self, destination: str) -> None:
        """Adopt `destination` as this snip's destination, then fire it.

        Firing immediately rather than only re-facing: the caret was opened
        to finish the snip a different way, and leaving the user to press
        the face afterwards would make choosing a destination cost two
        clicks where the face alone costs one.

        The *chooser* is updated too, not just the button's face, and that
        is the half this used to miss. `outcome` -- which `app.py`'s
        `_on_captured` reads to decide whether the review window opens --
        is the chooser's value, not the bar's. So a caret that re-faced the
        button alone left the destination picked before the snip still in
        force: under a face reading `Open`, choosing Copy put the image on
        the clipboard and then opened the review window anyway, and
        choosing Save wrote the file and opened it too. The menu looked
        broken while doing exactly what it said. Reported as "cant seem to
        change the option here? it just always open it in the editor".

        Set before the action fires, not after: `_on_bar_copy`/`_on_bar_save`
        run the whole capture synchronously, `_report_capture` included, so
        an update afterwards would land after the window it was meant to
        suppress had already opened.

        `_on_bar_open` performs the same sync for its own face-click path
        (`set_after("review")`) and is left alone -- the two routes agree,
        and the face click is the one that was never wrong.
        """
        self._bar.set_destination(destination)
        after = self._DESTINATION_OUTCOMES.get(destination)
        if after is not None:
            # A per-snip override, exactly like the chooser's own: this
            # deliberately does not write back to Settings, per the
            # handoff's rule that the per-snip control may differ from the
            # stored preference without changing it.
            self._chooser.set_after(after)
        self._bar._on_destination_activated(destination)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        # The ants only cost frames while actually on screen -- the
        # acceptance criterion is explicit that the timer must not keep
        # ticking (and scheduling repaints) once the overlay is hidden.
        self._ants_timer.start()
        # A selection set before this window was ever shown (e.g. a mode
        # that seeds one at construction time) needs the bar to catch up
        # now that `self.isVisible()` has actually become true.
        self._sync_bar_visibility()
        # Likewise the HUD: `_hints_enabled` defaults on and may already be
        # true before this window was ever shown.
        self._sync_hud_visibility()
        # The close button (SNX-80): unconditional, unlike the two syncs
        # above -- it has no preference or selection state to check, it is
        # simply on for as long as this window is.
        # Placed again here, not just in __init__: with nothing selected
        # `_chrome_bounds` follows the pointer, which may have moved between
        # building this window and showing it.
        self._reposition_close_button()
        self._close_button.show()
        self._sync_chooser_visibility()

    def hideEvent(self, event) -> None:
        super().hideEvent(event)
        self._ants_timer.stop()
        self._bar.hide()
        self._style_popover.hide()
        self._tool_hint.hide()
        self._popover.hide()
        for menu in self._family_menus.values():
            menu.hide()
        self._toast.hide()
        self._hud.hide()
        self._close_button.hide()
        self._chooser.hide()

    def closeEvent(self, event) -> None:
        # Transparent before the unmap, never after. Mutter stages an unmap
        # the way it stages a map -- scaling the window down and away --
        # which over a frozen desktop is the expanding page in reverse, and
        # just as wrong: finishing or cancelling a snip should hand the
        # desktop back, not play something on the way out. Setting opacity
        # here means the compositor has the property before it is asked to
        # withdraw the window, so it shrinks something already invisible.
        #
        # Done synchronously rather than by deferring the close a frame:
        # `close()` is what tells `AppController` the session is over, and a
        # close that only takes effect a few milliseconds later would let a
        # second shortcut press inside that gap be refused as "an overlay is
        # already open".
        self.setWindowOpacity(0.0)

        # Deliberately not hideEvent: `_start_delayed_capture` (SNX-50)
        # also plain-hides this same window mid-countdown and re-shows it
        # in place a moment later, which must not tear down this window's
        # own `_MonitorVeil` companions (if any) -- only an actual close()
        # (today, only the second stage of Esc) means the session itself
        # is over.
        super().closeEvent(event)
        if self._on_dismissed is not None:
            self._on_dismissed()

    # How long to stay invisible while the compositor plays its map
    # animation. GNOME's is in this range; erring slightly long costs a few
    # imperceptible milliseconds, erring short lets the tail of the scale-up
    # show, which is the whole point of the exercise.
    _REVEAL_DELAY_MS = 220


    def _reveal(self) -> None:
        """Full opacity, once the compositor has finished staging the map.

        Guarded on still being visible: Esc, or a second request forwarded
        in, can close this window inside the delay, and reviving a closed
        overlay by setting its opacity would be worse than the animation
        ever was.
        """
        if self.isVisible():
            self.setWindowOpacity(1.0)

    def show_on_screen(self, screen: QScreen | None) -> None:
        """Show this window, positioned for whichever session type the
        caller (`open_overlay`) already detected -- never assumed here.

        `screen` is None for X11 (and for any caller with no real `QScreen`
        to hand, e.g. the offscreen platform tests run under): plain
        `show()`, which lands this window at the geometry `__init__`
        already set via `setGeometry(frame.logical_origin, ...)` -- X11
        honours a client's requested position, so that alone is correct
        and unchanged from before this ticket.

        `screen` given means Wayland: a client there cannot choose its own
        window's position at all (SNX-58) -- the compositor decides, and
        a plain shown window routinely lands away from the real pixels the
        frame was captured from, which is what read as the desktop
        appearing twice, shifted. Fullscreen is the one state whose
        placement *is* the compositor's job, guaranteed to match `screen`
        exactly, so this requests that instead of a plain shown geometry.
        `winId()` forces the native window to exist first -- `windowHandle()`
        is None until it does -- so `setScreen` has something to act on.
        """
        if screen is None:
            # Rendered once before mapping: `grab()` runs a full paintEvent
            # into an offscreen pixmap, so the backing store already holds
            # the frozen frame when the window appears rather than being
            # filled on the first exposure.
            self.grab()
            # Mapped transparent, then revealed. Mutter stages a newly
            # mapped window by scaling it up into place, and over a frozen
            # desktop that reads as a page expanding across the very area
            # being captured -- a recording of it shows a shrunken copy of
            # the desktop sliding outwards. The animation cannot be turned
            # off per window, and the one flag that skips it entirely
            # (X11BypassWindowManagerHint) costs this window the keyboard
            # focus it lives on -- measured, and recorded where the flags
            # are set. So the compositor plays it on something invisible.
            self.setWindowOpacity(0.0)
            self.show()
            QTimer.singleShot(self._REVEAL_DELAY_MS, self._reveal)
        else:
            self.winId()
            handle = self.windowHandle()
            if handle is not None:
                handle.setScreen(screen)
            self.setWindowOpacity(0.0)
            self.showFullScreen()
            QTimer.singleShot(self._REVEAL_DELAY_MS, self._reveal)
        # Above every other window and focused the moment it opens, per
        # the acceptance criterion -- WindowStaysOnTopHint alone (set in
        # __init__) keeps it on top but doesn't itself force keyboard
        # focus, particularly right after a fullscreen state change.
        self.raise_()
        self.activateWindow()

    # -- keyboard shortcuts (SNX-47) -----------------------------------------
    # docs/design/overlay-redesign.md's "Keyboard" table is the authority: a
    # tool letter from tokens.SHORTCUTS, Ctrl+Z / Ctrl+Shift+Z for undo/redo,
    # Enter to copy-and-dismiss, and Esc, whose second stage the table
    # explicitly leaves for us to decide -- see _handle_escape. The tool
    # letters, Enter and `?` are suppressed outright while a text label or a
    # slider has focus, per the table's own closing line -- see
    # _shortcuts_suppressed. SNX-65 adds one more the table predates: `?`
    # toggles `_hints_enabled`, the reachable-without-a-file escape hatch for
    # the shortcut list now that the HUD it lives in is off by default.
    #
    # SNX-79: Escape and undo/redo are carved out of that suppression.
    # Touching the stroke slider is part of ordinary use, and a focused
    # slider or label must never leave the user with no way to close this
    # modal, full-screen window, nor with a broken undo stack -- see
    # keyPressEvent, which checks for those before _shortcuts_suppressed()
    # rather than after.

    def _shortcuts_suppressed(self) -> bool:
        """True while keyboard focus is on a widget these shortcuts must
        leave alone: a slider or a text-editing widget -- `QLineEdit` is
        what the text tool's own label editor (`_text_edit`) is, per
        `shapes.Text`'s docstring, mirroring editor.py's
        `Canvas._ensure_text_edit`. The style popover's own sliders never
        take focus, for exactly this reason: its keys have to keep working
        while it is open.

        `self.focusWidget()`, not the process-wide `QApplication.
        focusWidget()`, is enough here: every widget these shortcuts must
        yield to lives inside this window, and `QWidget.focusWidget()`
        reports a child that's been given focus via `setFocus()` regardless
        of whether this window itself is ever shown -- which is what lets a
        test give a slider focus without a real, visible window.
        """
        focus = self.focusWidget()
        return isinstance(focus, (QSlider, QLineEdit))

    def _handle_escape(self) -> None:
        """Two-stage Esc -- the decision the spec leaves to us: "in the real
        app decide whether Esc should also dismiss the overlay, and if so
        make it two-stage (ink first, then close)." While any ink is
        present, the first press only discards it (`discard()`, which
        toasts "Ink discarded") and leaves the overlay open so re-framing
        can continue; once there is nothing left to discard, the next press
        closes the overlay without capturing -- `Overlay`'s own Escape
        above is unconditional cancel because it has no ink to lose first.
        """
        if self._marks:
            self.discard()
        elif self._selection is not None and not self._armed_for_recording:
            # Back a stage, not out. The handoff's post-selection bars carry
            # no mode control -- its legend reads "Esc back" -- so once the
            # chip left this bar, Esc became the only way to reach the mode
            # again. Cancelling the whole snip instead would mean a
            # mis-picked mode cost the selection too.
            #
            # A recording that is already armed is excluded: `app.py` holds
            # state for it, and its own Esc path cancels that properly
            # rather than leaving a bar attached to a region this window
            # just dropped.
            self.set_selection(None)
            self._chooser.reopen()
            self._sync_chooser_visibility()
        else:
            self.close()

    def _abandon_text_entry(self, label: QLineEdit) -> None:
        """Escape's first stage while a label is focused (SNX-79): empties
        and hides the field instead of committing it as a Text mark, so
        typing a word into a label and then hitting Escape abandons that
        label rather than saving it.

        Clearing the text before hiding matters for the real text-tool
        editor (`_text_edit`): hiding a focused `QLineEdit` fires
        `editingFinished` synchronously (see `_commit_text`'s own
        docstring), and `_commit_text` only calls `add_mark`
        `if self._text_edit.text():` -- with the field already emptied that
        guard is false, so the existing commit path discards the label
        itself instead of this needing a second, parallel way to do it.
        Hiding also returns keyboard focus to this window (Qt's normal
        behaviour when a focused child is hidden -- see `_shortcuts_
        suppressed`'s use of `focusWidget()`), which is what lets the
        *next* Escape reach `_handle_escape` directly instead of bubbling
        back through here.

        Takes the focused label as a parameter rather than reading
        `self._text_edit` because `TestKeyboardShortcutSuppression` (SNX-47)
        stands in a bare `QLineEdit` for the real editor to test suppression
        generically -- this must abandon whichever label actually has focus.
        """
        self._text_editor.abandon()

    def keyPressEvent(self, event) -> None:
        key = event.key()
        modifiers = event.modifiers()

        # Ahead of the chooser, which reads a bare Escape as cancelling the
        # snip: with Full screen armed it leaves the mode instead -- see
        # `_leave_monitor_mode`.
        if key == Qt.Key.Key_Escape and self._picking_monitor:
            self._leave_monitor_mode()
            return

        # The chooser's shortcuts are live for as long as it is -- R/W/F/L
        # to switch mode, Space to reopen it, Esc to close a menu. It gets
        # first refusal while there is no selection, and returns False for
        # anything that is not its own.
        if (
            self._selection is None
            and not self._shortcuts_suppressed()
            and self._chooser.handle_key(key, event.text())
        ):
            return

        # Escape is the way out of this modal, full-screen window and must
        # work regardless of which child holds focus (SNX-79) -- checked
        # ahead of _shortcuts_suppressed() so a focused slider or label never
        # swallows it. Reaching this method at all already means the focused
        # child declined the key itself: neither QSlider nor a plain
        # QLineEdit handles Escape, so Qt's normal unhandled-key propagation
        # bubbles it up here exactly as if nothing had focus.
        if key == Qt.Key.Key_Escape:
            focus = self.focusWidget()
            if isinstance(focus, QLineEdit):
                self._abandon_text_entry(focus)
            # A menu that is open is what Esc closes first, and closing it
            # costs nothing else.
            elif not self._close_bar_menus():
                self._handle_escape()
            return

        # Same SNX-79 carve-out for undo/redo. A label's own Ctrl+Z /
        # Ctrl+Shift+Z never reaches this method in the first place --
        # QLineEdit handles those itself, for its own text-undo, before they
        # can bubble -- so this only ever runs here with a slider (or
        # nothing) focused, and no isinstance check is needed the way Escape
        # above needed one.
        #
        # Exact-equality modifier check, not bitwise: Ctrl+Shift+Z's
        # modifier set includes ControlModifier, so a bitwise "is Control
        # held" test would swallow every redo as an undo. Same trap
        # editor.py's own `_undo_redo_action` docstring documents.
        if key == Qt.Key.Key_Z and modifiers == (
            Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier
        ):
            self.redo()
            return
        if key == Qt.Key.Key_Z and modifiers == Qt.KeyboardModifier.ControlModifier:
            self.undo()
            return

        if self._shortcuts_suppressed():
            super().keyPressEvent(event)
            return

        if key in (Qt.Key.Key_Enter, Qt.Key.Key_Return):
            # Enter fires the stage's primary action, and on a recording
            # that is Record -- not Copy. Without this branch it took a
            # *screenshot* of the region a recording was being set up
            # around and put that on the clipboard, which is the wrong
            # capture, the wrong destination and the wrong kind entirely.
            if self._armed_for_recording:
                if self._on_recording_start is not None:
                    self._on_recording_start()
                return
            # Mirrors Overlay's own Enter handling above: nothing to copy
            # (and dismiss) without a selection yet.
            if self._selection is not None:
                self.copy()
                self.close()
            return

        if self._bar.handle_tool_key(key):
            return

        # 1-7, [ ] and D style the active tool, whether the popover is open
        # or not, and never close it.
        if self._style_popover.handle_key(key):
            return

        if key == Qt.Key.Key_Question:
            # SNX-65: hints default off now (see the "Top hint HUD" comment
            # block above `HintHUD`), so this is the escape hatch that keeps
            # the full shortcut list reachable without editing a file --
            # every button's own tooltip already names its own key, but `?`
            # is the one place the whole list reads together.
            self.set_hints_enabled(not self._hints_enabled)
            return

        super().keyPressEvent(event)

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
        # A press while a drag is still open means that drag's release never
        # arrived. It ends here, as it stands, before this press can start
        # anything of its own.
        self._end_drags()
        if event.button() != Qt.MouseButton.LeftButton:
            return
        if self._popover.isVisible() and not self._popover.geometry().contains(
            event.position().toPoint()
        ):
            # Per the spec's "clicking outside the popover closes it
            # without changing the mode": the click is consumed by the
            # dismissal alone and returns here, rather than falling through
            # to the handle/eraser logic below, so it can't also start a
            # resize or an erase underneath the popover in the same press.
            self._popover.hide()
            return
        if self._close_bar_menus():
            # A press outside the bar's open menu or popover closes it and
            # does nothing more. A press *on* one never gets this far -- see
            # `_Chrome` -- or this line would close the menu before the row
            # under the pointer could take the click.
            return
        if self._picking_window:
            # A press while armed is always a pick, never a resize or a
            # stroke -- returns unconditionally, the same "stop event
            # propagation" rule the handle branch below already follows.
            self._confirm_window_pick(event.position())
            return
        if self._picking_monitor:
            # The same rule for Full screen's monitor pick (#53).
            self._confirm_monitor_pick(event.position())
            return
        handle = self._handle_at(event.position())
        if handle is None:
            # `not self._recalled_selection`: a rectangle this window
            # recalled by itself is a suggestion, not a selection, so a
            # press inside it starts a fresh drag exactly as a press on
            # bare overlay does. Without this the preference was a trap --
            # a recalled region covering a whole monitor left nowhere
            # outside it to press, so there was no way to frame anything
            # else short of Esc. The handles still resize it (this branch
            # is only reached when `_handle_at` found none), and adopting
            # it any other way clears the flag.
            if (
                self._selection is not None
                and not self._recalled_selection
                and QRectF(self._selection).contains(event.position())
            ):
                if self._eraser_active:
                    # Armed for the whole press so the eraser can be swept
                    # across a group of marks rather than aimed at each one
                    # -- see `mouseMoveEvent`. The spec's "eraser -- no
                    # drag" meant it draws nothing, not that it may only be
                    # clicked; rubbing out is a sweep everywhere else it
                    # exists. A miss is a safe no-op inside erase_at.
                    self._erasing = True
                    self.erase_at(event.position())
                else:
                    self._start_stroke(event.position())
            else:
                # SNX-57: Region -- the default mode, armed by nothing above
                # -- gets no selection at all otherwise: Window and Full screen
                # each set one before a plain press could ever reach here. A press on the empty overlay starts an
                # ordinary rectangle drag, the same press-drag-release shape
                # `Overlay`'s own RECTANGLE mode already uses.
                #
                # A press *outside* an existing selection starts a new one
                # the same way, rather than being the no-op it used to be.
                # Getting a selection slightly wrong is the common case, and
                # the only way out of one was Esc -- which cancels the whole
                # snip, frozen frame and all, so a misplaced drag cost the
                # user the entire capture and a fresh trip through the tray.
                # Every other snipping tool lets a press on the dimmed area
                # start over, and the resize handles are unaffected: this
                # branch is only reached when `_handle_at` found none, so
                # nudging an edge still resizes rather than restarting.
                #
                # Marks are deliberately left alone. They live in window
                # coordinates, not selection-relative ones, so they stay
                # exactly where they were drawn; whatever the new selection
                # covers is captured, and re-selecting the old region brings
                # them all back. Clearing them here would make a stray click
                # destroy annotation work that Ctrl+Z could not bring back.
                self._region_drag_anchor = event.position()
                self._selection_anchor = event.position()
                self.set_selection(QRect(event.position().toPoint(), QSize(0, 0)))
            return
        # Per the spec: a handle press is a resize, never a stroke, and
        # returning here means nothing past this point runs for it.
        # Dragging an edge is adopting the rectangle, recalled or not.
        self._recalled_selection = False
        self._active_handle = handle
        self._resize_anchor = QRect(self._selection)

    def _start_stroke(self, pos: QPointF) -> None:
        """Begin a mark for whichever tool `self._bar.active_tool` names,
        at `pos` (this widget's own window coordinates -- the same space
        `_marks` lives in). Only ever reached from `mousePressEvent` for a
        press that missed every resize handle, landed inside the
        selection, and found the eraser disarmed.

        docs/design/overlay-redesign.md's "Drawing" is the spec for what
        each tool does here: pen/highlighter/arrow/rect/blur arm
        `_in_progress_shape` for `_extend_stroke`/`mouseReleaseEvent` to
        grow and commit; step commits a `StepMarker` immediately, on the
        press alone, per its own "click only" entry; text opens its label
        editor instead of arming a drag (`_start_text_entry`), since that
        tool's whole gesture is the click too. A `tool` of `None` (nothing
        picked in the bar yet, or the eraser -- handled by the caller
        before this is ever reached) or any other unrecognised string is a
        no-op, mirroring editor.py's own `Canvas._new_in_progress_shape`
        guard on `self._tool is None`.
        """
        tool = self._bar.active_tool
        style = self._styles.of(tool)
        colour = QColor(style.colour)

        # One factory, shared with the review window's Annotate mode -- see
        # snipux/marks.py. `step` and `text` fall through to their own
        # click-only handling below, which is why `begin_stroke` returns
        # None for them rather than pretending they are drags.
        started = begin_stroke(
            tool,
            pos,
            colour=colour,
            stroke_width=style.size,
            blur_strength=style.strength,
            fill=style.fill,
            dash=style.dash,
        )
        if started is not None:
            self._in_progress_shape = started
        elif tool == "step":
            # Click only -- no drag, no `_in_progress_shape`, per the spec.
            self.add_mark(
                StepMarker(
                    colour=colour,
                    stroke_width=style.size,
                    point=pos,
                    number=next_step_number(self._marks),
                )
            )
            return
        elif tool == "text":
            self._start_text_entry(pos, colour)
            return
        else:
            return

        self.update()  # something to show from the very first pixel

    def _extend_stroke(self, pos: QPointF) -> None:
        """Grow `_in_progress_shape` to `pos`: append a point for a
        freehand stroke (pen/highlighter), or move its `end` for a
        two-point one (arrow/rect/blur) -- the same split editor.py's
        `Canvas.mouseMoveEvent` makes, since both classes of shape share
        the same field names (see shapes.py's `_transformed` docstring).
        """
        extend_stroke(self._in_progress_shape, pos)
        self.update()

    # -- text tool (SNX-52) -------------------------------------------------

    @property
    def _text_edit(self):
        """The label editor's live field, or None before the first label.

        A view onto `_text_editor`, which owns it -- kept because
        `keyPressEvent` and `_shortcuts_suppressed` both need to know
        whether a focused widget is that field, and this reads better at
        those two sites than reaching through the editor.
        """
        return self._text_editor.field

    def _commit_text(self) -> None:
        """Commit whatever is in the label editor. Delegates; see
        `marks.TextLabelEditor.commit`.
        """
        self._text_editor.commit()

    def _start_text_entry(self, pos: QPointF, colour: QColor) -> None:
        """Open the text tool's label editor at `pos`, seeded with a
        placeholder and focused for immediate typing, per
        docs/design/overlay-redesign.md's "Drawing": "text -- click drops
        an editable label seeded with Label, focused for immediate
        typing." Mirrors editor.py's `Canvas.mousePressEvent` handling of
        `Tool.TEXT` -- the label commits later, via `_commit_text`, never
        here.

        SNX-77: a click that lands away from the field never blurs it --
        the shared `_text_edit` stays focused right through
        `mousePressEvent`/`_start_stroke` -- so nothing forces the
        `editingFinished` this label editor relies on to commit. Committing
        explicitly here, before touching `_pending_text_*` or clearing the
        field, is what a second (or third...) label needs to survive:
        `_commit_text` reads whatever was typed into the *still-live*
        field against the *still-old* pending point/colour/stroke-width,
        so the label just finished keeps the position and styling it was
        typed at rather than picking up this click's. Only once that's
        settled do the pending fields move on to this new click, and only
        then does the field get cleared and re-shown for it -- so an empty
        field (nothing typed yet, or the very first label ever) still has
        nothing to commit, per `_commit_text`'s own guard.
        """
        self._text_editor.begin(pos, colour, self._styles.of("text").size)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._dragging() and not event.buttons() & Qt.MouseButton.LeftButton:
            # Nothing is held, so the drag lost its release somewhere --
            # outside the window, to a grab, to anything. Without this,
            # ordinary movement would go on stretching the mark.
            self._end_drags()
        if self._erasing:
            self.erase_at(event.position())
            return
        # SNX-48: tracked on every move regardless of mode, so
        # `_select_full_screen` always has a recent position to answer
        # "which display is the cursor on" from -- see its own docstring
        # for why this beats `QCursor.pos()`.
        self._cursor_pos = event.position()
        # Before anything is selected the chooser row belongs on whichever
        # monitor is being looked at, which changes as the pointer crosses
        # a bezel -- see `_follow_pointer_to_its_monitor`.
        self._follow_pointer_to_its_monitor()

        if self._picking_window:
            # Live preview while armed: a hit sets `_selection` to that
            # window's rect, a miss clears it -- mirrors `Overlay`'s own
            # "a miss actively clears any previously-shown preview instead
            # of leaving it stuck." None of the resize/stroke/cursor logic
            # below applies while picking, so this returns unconditionally.
            found = self._geometry_provider.window_named_at(
                self._to_absolute(event.position())
            )
            self._hovered_window = found
            rect = None if found is None else found[1]
            self.set_selection(self._to_local_rect(rect).toRect() if rect is not None else None)
            self.setCursor(Qt.CursorShape.CrossCursor)
            super().mouseMoveEvent(event)
            return

        if self._picking_monitor:
            # Full screen's preview follows the pointer across a bezel
            # (#53). Repainted only when the monitor under it changes, since
            # this runs on every pixel of every move; a gap between monitors
            # highlights nothing.
            monitor = self._monitor_containing(self._to_absolute(event.position()))
            if monitor != self._hovered_monitor:
                self._hovered_monitor = monitor
                self.update()
            self.setCursor(Qt.CursorShape.CrossCursor)
            super().mouseMoveEvent(event)
            return

        if self._region_drag_anchor is not None:
            # SNX-57: same "handled here, nothing else runs" shape the
            # Window branch above already uses for its own
            # in-progress picks -- a rectangle drag-to-create is never also
            # a resize or a stroke while it's live.
            # QRectF's two-point constructor, not QRect's -- QRect(p1, p2)
            # treats both points as inclusive corners and would report one
            # pixel more of width/height than the cursor has actually
            # travelled. Mirrors `Overlay`'s own RECTANGLE-mode drag above
            # (`QRectF(self._drag_anchor, absolute_pos).normalized()`).
            rect = QRectF(self._region_drag_anchor, event.position()).normalized()
            self.set_selection(rect.toRect())
            self.setCursor(Qt.CursorShape.CrossCursor)
            super().mouseMoveEvent(event)
            return

        if self._in_progress_shape is not None:
            self._extend_stroke(event.position())
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
            self._apply_idle_cursor()
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            self._erasing = False
            return
        self._end_drags(event.position())

    def event(self, event) -> bool:
        # Focus leaving the window takes any release with it.
        if event.type() == QEvent.Type.WindowDeactivate:
            self._end_drags()
        return super().event(event)

    def _dragging(self) -> bool:
        return (
            self._in_progress_shape is not None
            or self._erasing
            or self._active_handle is not None
            or self._region_drag_anchor is not None
            or self._bar.is_dragging
        )

    def _end_drags(self, pos: QPointF | None = None) -> None:
        """End whatever pointer drag is open: commit the mark being drawn,
        stop a sweep or a reframe, or confirm the region being dragged out.

        A release is only one way a drag ends. A release can be lost -- to a
        grab, to the pointer leaving the window, to focus going elsewhere --
        and a drag left open turns ordinary movement into stretching the
        mark until it covers the selection, which looks like a rendering
        fault and is not one. So this also runs for a move with no button
        held, for a press while a drag is still open, and for the window
        losing focus.

        Everything is read from the drag as it stands now, never from what
        it was when its press began: a mark keeps the end its last move gave
        it. `pos` is where a release landed, when there was one.

        A drag of the bar itself is one of these too. Its moves go to the
        bar while its button is held, but a move with no button held comes
        here -- the bar tracks no hover of its own -- as do a press on the
        frame and the window losing focus.
        """
        self._bar.end_drag()
        self._erasing = False
        self._active_handle = None
        self._resize_anchor = None
        if self._region_drag_anchor is not None:
            # SNX-57: a Region drag-to-create always commits or discards,
            # and never falls through to the stroke below.
            self._confirm_region_drag(pos)
            return
        shape, self._in_progress_shape = self._in_progress_shape, None
        if shape is None:
            return
        committed = finalize_mark(shape)
        if committed is not None:
            self.add_mark(committed)
        else:
            # Below the spec's minimum size -- discarded, not committed
            # (shapes.finalize_mark's own docstring is the authority for
            # which shapes/thresholds that covers). Still needs a repaint:
            # `_paint_marks` was showing this shape's live preview up to the
            # instant it ended.
            self.update()

    def _confirm_region_drag(self, pos: QPointF | None) -> None:
        """End a Region-mode drag-to-create at `pos` (window coordinates)
        and either commit or discard it. Only ever reached from
        `_end_drags` while `_region_drag_anchor` is set.

        Discards below `tokens.Metric.SEL_MIN_W/H` -- the same floor
        `_resize_selection` already clamps re-framing to -- rather than
        committing an unusable sliver, per the ticket's own acceptance
        criterion; a plain click (no movement at all) is already well
        under that floor, so no separate misfire check is needed on top
        of it.
        """
        anchor = self._region_drag_anchor
        self._region_drag_anchor = None
        if pos is None:
            # The release was lost, so there is no point to end at: the
            # rectangle the last move drew is the drag as it stands.
            if self._selection is None:
                return
            rect = QRect(self._selection)
        else:
            # QRectF's two-point constructor, not QRect's -- see
            # `mouseMoveEvent`'s own comment above on why the inclusive-corner
            # one would over-report by a pixel on each axis.
            rect = QRectF(anchor, pos).normalized().toRect()
        metric = design.tokens.Metric
        if rect.width() < metric.SEL_MIN_W or rect.height() < metric.SEL_MIN_H:
            self.set_selection(None)
            return
        self._commit_selection(rect)

    def _resize_selection(self, pos: QPointF) -> None:
        """Apply one drag-move of `self._active_handle` to the selection.

        `self._resize_anchor` is the selection as it stood when the drag
        started; edges this handle doesn't free are read from it and never
        written below, which is what keeps the opposite edge/corner
        anchored for the whole drag. Clamps are applied in the order the
        README's "Re-framing" section gives -- minimum size, `x >= 0`,
        `y >= 52`, stays inside the window, room for the floating bar --
        with two deliberate deviations from the spec: the minimum is
        `tokens.Metric.SEL_MIN_W/H` (16x16, not the spec's 200x140), the
        floating-bar clamp gives way to that minimum instead of the other
        way round, and (SNX-65) the `y >= 52` clearance itself only applies
        while `_hints_enabled` is true -- with the HUD off there is nothing
        left at the top to stay clear of.
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
        # 3. y >= 52, clear of the top hint HUD -- but only while the HUD
        # is actually the preference the user has on. SNX-65 turned hints
        # off by default, and holding this 52px strip clamped shut for a
        # bar nobody is shown would just deny the selection room the AC
        # explicitly asks it get back.
        if free_top:
            top_clearance = self._TOP_CLEARANCE if self._hints_enabled else 0.0
            top = max(top, top_clearance)
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
        # Layer 1: the frozen frame, full window -- with every committed
        # Blur/Pixelate mark's effect already baked in (SNX-63; see
        # `_base_layer_image`), not the raw capture. This is also what
        # keeps the selection "undimmed and at 1:1": the scrim below
        # punches a hole out of this exact drawImage call rather than
        # compositing a second one, so the hole shows precisely these
        # pixels, obscuring marks included, untouched.
        painter.drawImage(QRectF(self.rect()), self._base_layer_image())
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
            # Chips (SNX-43): last in the "selection" layer's paint order,
            # per the README's "undimmed pixmap, ink layer, frame stroke,
            # handles, chips" -- and never part of `rendered_image()`'s own
            # export path, which flattens `_marks` onto the frame directly
            # and never calls this method, so neither chip can leak into a
            # save/copy.
            self._paint_dimension_chip(painter)
            self._paint_frozen_pill(painter)
        if self._picking_window and self._hovered_window is not None:
            self._paint_window_hover(painter)
        if self._picking_monitor and self._hovered_monitor is not None:
            self._paint_monitor_hover(painter)
        painter.end()

    def _paint_window_hover(self, painter: QPainter) -> None:
        """The Window-mode preview: an accent outline over the window under
        the pointer, with a chip naming it and its size.

        Accent rather than the selection's white, because nothing has been
        chosen yet -- this is what a click *would* take. The name is the
        half a rectangle cannot say: two same-sized windows look identical
        outlined, and picking the wrong one only shows up afterwards.
        """
        title, absolute = self._hovered_window
        rect = QRectF(self._to_local_rect(absolute))
        self._paint_pick_highlight(
            painter,
            fill=rect,
            outline=rect,
            title=title,
            size=absolute.size(),
            chip_origin=QPointF(rect.left(), rect.top() - design.tokens.Metric.CHIP_OFFSET_Y),
        )

    def _paint_monitor_hover(self, painter: QPainter) -> None:
        """Full screen's preview on a desk with more than one monitor (#53):
        Window mode's highlight, over the monitor under the pointer.

        Placed so all of it is on the monitor it names. The 2px outline is
        drawn inside the edge, where Window mode's straddles it: a monitor's
        edge is a bezel, and half the line would land on the neighbour. The
        chip sits inside the usable top-left corner, the close button's
        margin in, rather than above the rect -- which is the monitor above,
        or a gap no monitor shows.

        The chip gives the size alone. A window needs its title because two
        same-sized windows can sit in the same place; two monitors cannot,
        and where the highlight is already says which one it is.
        """
        monitor = self._hovered_monitor
        rect = QRectF(self._to_local_rect(monitor))
        usable = QRectF(self._to_local_rect(self._usable_area(monitor)))
        margin = self._CLOSE_BUTTON_MARGIN
        self._paint_pick_highlight(
            painter,
            fill=rect,
            outline=rect.adjusted(1, 1, -1, -1),
            title="",
            size=monitor.size(),
            chip_origin=usable.topLeft() + QPointF(margin, margin),
        )

    def _paint_pick_highlight(
        self,
        painter: QPainter,
        *,
        fill: QRectF,
        outline: QRectF,
        title: str,
        size: QSizeF,
        chip_origin: QPointF,
    ) -> None:
        """The accent fill, outline and chip over what a click would take,
        shared by Window and Full screen's previews. Rects and the chip's
        origin are window-local; `size` is the target's own, for the chip.
        """
        painter.setBrush(design.flow_color("WINDOW_HOVER_FILL"))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRect(fill)

        pen = QPen(design.flow_color("WINDOW_HOVER"))
        pen.setWidth(2)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(outline)

        dimensions = f"{round(size.width())} × {round(size.height())}"
        label = f"{title} — {dimensions}" if title else dimensions
        font = QFont(design.font_families().ui)
        font.setPixelSize(12)
        font.setWeight(QFont.Weight(600))
        metrics = QFontMetricsF(font)
        chip = QRectF(
            chip_origin.x(),
            chip_origin.y(),
            metrics.horizontalAdvance(label) + 20,
            22,
        )
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(design.flow_color("ACCENT"))
        painter.drawRoundedRect(chip, 6, 6)
        painter.setFont(font)
        painter.setPen(design.flow_color("ACCENT_FG"))
        painter.drawText(chip, int(Qt.AlignmentFlag.AlignCenter), label)

    def _window_to_frame_scale(self) -> tuple[float, float]:
        """Ratio of `self._frame.image`'s own pixel size to this widget's
        window-local (logical) one, along each axis -- the same conversion
        `Overlay._paint_magnifier` above already uses to map a window/
        logical point into the frozen frame's own pixel space. Above 1 on
        any display with a device pixel ratio above one, where the
        captured frame carries more pixels than the window's logical size.

        Measured against `self.rect()` rather than `self._frame.
        logical_size` -- the two are set equal at construction (
        `setGeometry`, `_finish_delayed_capture`), but `self.rect()` is
        what Layer 1's own `drawImage(QRectF(self.rect()), ...)` scales
        the frame against, so an obscuring mark's sampled rect has to
        agree with exactly that source of truth, not a merely-equal
        second one.
        """
        widget_rect = self.rect()
        if widget_rect.width() <= 0 or widget_rect.height() <= 0:
            return 1.0, 1.0
        return (
            self._frame.image.width() / widget_rect.width(),
            self._frame.image.height() / widget_rect.height(),
        )

    def _base_layer_image(self) -> QImage:
        """Layer 1's own source pixels: `self._frame.image` with every
        committed `ObscuringShape` mark's blur/pixelate effect actually
        baked in, in list order -- each one sampling the output of every
        obscuring mark before it, the same left-to-right composition
        `shapes.render()` uses for export, via repeated `apply()` calls
        rather than `render()` itself (which would also flatten every
        *non*-obscuring mark permanently into the image, losing the
        live, undo-able painter drawing `_paint_marks` below still wants
        for those).

        Fixes SNX-63: `_paint_marks` used to skip every `ObscuringShape`
        outright with a "later ticket" comment that no later ticket ever
        picked up, so a committed blur/pixelate was invisible until
        export. Baking it into Layer 1 here instead of painting it inside
        `_paint_marks` is what keeps it aligned across a re-frame for
        free -- this method never reads `_selection` at all, only
        `_marks` and `_frame`, so the obscured patch sits over exactly
        the pixels it was drawn on regardless of where the selection
        rect (and its clip) currently is.

        Cached against `key` -- each obscuring mark's class, geometry and
        `strength`, plus the frame's own identity -- and only recomputed
        when that key actually changes, per this ticket's "does not
        visibly stall" acceptance criterion: a repaint triggered by
        something unrelated (an in-progress stroke elsewhere, marching
        ants, a resize drag) must not redo the scale-down/scale-up
        sampling for every obscuring mark on every single frame. Keying
        on `strength` rather than just object identity is also what
        makes an already-committed mark's on-screen look track a change
        to its own `strength` (e.g. a direct write to the mark) on the
        very next repaint, instead of it staying stuck at whatever this
        method last cached.

        A mark's `start`/`end` are in this widget's own window-local
        coordinates (see the class docstring) and need converting into
        `self._frame.image`'s own, larger-under-HiDPI pixel space before
        `apply()` -- which expects both its rect and the image it samples
        to share one coordinate space -- can sample the right pixels; see
        `_window_to_frame_scale`.
        """
        obscuring = [shape for shape in self._marks if isinstance(shape, ObscuringShape)]
        key = (
            id(self._frame),
            tuple(
                (
                    type(shape).__name__,
                    shape.start.x(),
                    shape.start.y(),
                    shape.end.x(),
                    shape.end.y(),
                    shape.strength,
                )
                for shape in obscuring
            ),
        )
        if self._base_layer_cache is not None and self._base_layer_cache[0] == key:
            return self._base_layer_cache[1]

        image = self._frame.image
        if obscuring:
            scale_x, scale_y = self._window_to_frame_scale()
            for shape in obscuring:
                image = replace(
                    shape,
                    start=QPointF(shape.start.x() * scale_x, shape.start.y() * scale_y),
                    end=QPointF(shape.end.x() * scale_x, shape.end.y() * scale_y),
                ).apply(image)

        self._base_layer_cache = (key, image)
        return image

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
        if not self._marks and self._in_progress_shape is None:
            return
        painter.save()
        painter.setClipRect(QRectF(self._selection))
        step_counter = 0
        for shape in self._marks:
            if isinstance(shape, StepMarker):
                step_counter += 1
                shape.number = step_counter
            if isinstance(shape, ObscuringShape):
                # Already baked into Layer 1 by `_base_layer_image` above,
                # in list order alongside every other obscuring mark --
                # painting it again here would double its effect, and
                # `draw()` isn't a real implementation to call anyway (see
                # its own docstring). An *in-progress* blur/pixelate drag
                # still gets a lightweight marquee instead -- see
                # `_paint_in_progress_shape` below -- baking a live preview
                # into Layer 1 on every mouse-move would be the exact
                # per-frame cost this ticket's performance criterion rules
                # out.
                continue
            shape.draw(painter)
        if self._in_progress_shape is not None:
            self._paint_in_progress_shape(painter, self._in_progress_shape)
        painter.restore()

    def _paint_in_progress_shape(self, painter: QPainter, shape: Shape) -> None:
        """Live preview of the mark `_start_stroke`/`_extend_stroke` are
        currently building -- everything but `StepMarker`/`Text`, which
        commit on press alone and never reach `_in_progress_shape` (see
        `_start_stroke`).

        `Rectangle`/`Arrow.draw()` already tolerate either corner order
        (see their own docstrings, "for the live in-progress preview"), so
        the shape's own `draw()` is enough for every tool but blur/
        pixelate: an `ObscuringShape` has no live preview of its own --
        `_paint_marks` above never draws a *committed* one either, for the
        same cost reason -- so this borrows `shapes.Crop`'s dashed-outline
        marquee instead, built from this shape's own colour/stroke/corners
        rather than the real (expensive, ordering-dependent) blur effect.
        """
        if isinstance(shape, ObscuringShape):
            Crop(
                colour=shape.colour,
                stroke_width=shape.stroke_width,
                start=shape.start,
                end=shape.end,
            ).draw(painter)
            return
        shape.draw(painter)

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

    # -- chips above the selection (SNX-43) ---------------------------------

    def _chip_font(self, spec: tuple[float, int], family: str) -> QFont:
        """A QFont at `spec`'s (pixel size, weight) in `family`.

        Built fresh rather than cached: both the rect-computing helpers
        below and their paint counterparts need one, and building it twice
        from the same `tokens.Font` entry is what keeps the size a rect was
        measured against and the size actually painted from ever drifting
        apart -- there's no cached QFont either could go stale against.
        """
        size, weight = spec
        font = QFont(family)
        font.setPixelSize(round(size))
        font.setWeight(QFont.Weight(weight))
        return font

    def _dimension_chip_texts(self) -> tuple[str, str]:
        """(`"1040 × 560"`, `"2 marks"`/`"1 mark"`) for the dimension chip.

        Recomputed from live state on every call -- never cached -- which is
        what makes the chip "update live while the selection is being
        resized" (the acceptance criterion): paintEvent calls this fresh on
        every repaint, and a resize drag, add_mark, undo, redo, clear or
        erase_at all end in `self.update()`, so the next paint always reads
        the current selection size and mark count. The mark count is
        singular at exactly one and plural otherwise, per the README's
        "1 mark" / "2 marks".

        Width/height come from `self._selection`, already this widget's own
        window-local *logical* pixels (see the class docstring) -- never
        `self._frame.image`'s larger pixel size. That's what keeps this
        correct on a display with device pixel ratio above one, per the
        README's HiDPI note: "the dimension chip should report logical
        selection size."
        """
        width = round(self._selection.width())
        height = round(self._selection.height())
        # A recording has no marks and never will -- there is no
        # annotate-in-place for a video (docs/design/recording.md) -- so
        # counting them says "0 marks" over a region the user is about to
        # film, in the vocabulary of a feature that does not apply to it.
        # The handoff's own live chip appends the frame rate here instead;
        # until that exists, the size alone is the honest half.
        if self._chooser.kind == "record":
            return f"{width} × {height}", ""
        count = len(self._marks)
        unit = "mark" if count == 1 else "marks"
        return f"{width} × {height}", f"{count} {unit}"

    def _dimension_chip_rect(self) -> QRectF:
        """Bounding rect of the dimension chip, in window coordinates:
        left-aligned to the selection's left edge, its own top edge
        `CHIP_OFFSET_Y` above the selection's top edge -- per the README's
        "left:0; top:-38px" -- sized to fit its current text exactly, the
        same way `_bracket_path`/`_edge_handle_rect` above expose their
        geometry separately from painting it.
        """
        metric = design.tokens.Metric
        sel = QRectF(self._selection)
        size_text, mark_text = self._dimension_chip_texts()
        mono = design.font_families().mono
        size_fm = QFontMetricsF(self._chip_font(design.tokens.Font.DIM_CHIP, mono))
        mute_fm = QFontMetricsF(self._chip_font(design.tokens.Font.DIM_CHIP_MUTE, mono))

        content_width = size_fm.horizontalAdvance(size_text)
        # The middot and the count are one unit: with no count there is
        # nothing to separate, and reserving their width would leave the
        # chip padded out around empty space.
        if mark_text:
            content_width += (
                self._CHIP_INNER_GAP
                + mute_fm.horizontalAdvance(self._CHIP_DOT)
                + self._CHIP_INNER_GAP
                + mute_fm.horizontalAdvance(mark_text)
            )
        content_height = max(size_fm.height(), mute_fm.height())
        width = content_width + 2 * self._CHIP_PAD_H
        height = content_height + 2 * self._CHIP_PAD_V
        return QRectF(sel.left(), sel.top() - metric.CHIP_OFFSET_Y, width, height)

    def _frozen_pill_rect(self) -> QRectF:
        """Bounding rect of the Frozen pill, in window coordinates:
        right-aligned to the selection's right edge, its own top edge
        `CHIP_OFFSET_Y` above the selection's top edge -- per the README's
        "right:0; top:-38px" -- sized to fit the pin icon and its label.
        """
        metric = design.tokens.Metric
        sel = QRectF(self._selection)
        ui = design.font_families().ui
        fm = QFontMetricsF(self._chip_font(design.tokens.Font.FROZEN, ui))

        content_width = (
            self._FROZEN_ICON_SIZE
            + self._FROZEN_INNER_GAP
            + fm.horizontalAdvance(self._FROZEN_LABEL)
        )
        content_height = max(self._FROZEN_ICON_SIZE, fm.height())
        width = content_width + 2 * self._CHIP_PAD_H
        height = content_height + 2 * self._CHIP_PAD_V
        top = sel.top() - metric.CHIP_OFFSET_Y
        return QRectF(sel.right() - width, top, width, height)

    def _paint_dimension_chip(self, painter: QPainter) -> None:
        """The left-hand chip: `WIDTH × HEIGHT`, a muted middot, then the
        mark count -- singular at one, per docs/design/overlay-redesign.md's
        "Chips above the selection".
        """
        mono = design.font_families().mono
        size_text, mark_text = self._dimension_chip_texts()
        size_font = self._chip_font(design.tokens.Font.DIM_CHIP, mono)
        mute_font = self._chip_font(design.tokens.Font.DIM_CHIP_MUTE, mono)
        size_fm = QFontMetricsF(size_font)
        mute_fm = QFontMetricsF(mute_font)

        rect = self._dimension_chip_rect()
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(design.color("CHIP_LIGHT_BG"))
        painter.drawRoundedRect(rect, self._CHIP_RADIUS, self._CHIP_RADIUS)

        # Both fonts share the same 12px pixel size (only the weight
        # differs), so a single baseline -- derived from the size text's own
        # ascent -- keeps every segment sitting on the same line rather than
        # each drawText computing (and risking disagreeing on) its own.
        baseline = rect.top() + self._CHIP_PAD_V + size_fm.ascent()
        x = rect.left() + self._CHIP_PAD_H

        painter.setFont(size_font)
        painter.setPen(design.color("CHIP_LIGHT_FG"))
        painter.drawText(QPointF(x, baseline), size_text)
        x += size_fm.horizontalAdvance(size_text) + self._CHIP_INNER_GAP

        # The middot separates two things. With no mark count to separate
        # it from -- a recording, which has no marks -- it is a dangling
        # "747 x 473 ·" that reads as a truncated label.
        if not mark_text:
            return

        painter.setFont(mute_font)
        painter.setPen(design.color("CHIP_DOT"))
        painter.drawText(QPointF(x, baseline), self._CHIP_DOT)
        x += mute_fm.horizontalAdvance(self._CHIP_DOT) + self._CHIP_INNER_GAP

        painter.setPen(design.color("CHIP_LIGHT_MUTE"))
        painter.drawText(QPointF(x, baseline), mark_text)

    def _paint_frozen_pill(self, painter: QPainter) -> None:
        """The right-hand pill: a pin glyph then the word "Frozen", telling
        the user the desktop behind the overlay is a still frame, not the
        live screen -- docs/design/overlay-redesign.md's "Overlay window"
        section.
        """
        ui = design.font_families().ui
        font = self._chip_font(design.tokens.Font.FROZEN, ui)
        fm = QFontMetricsF(font)

        rect = self._frozen_pill_rect()
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(design.color("CHIP_DARK_BG"))
        painter.drawRoundedRect(rect, self._CHIP_RADIUS, self._CHIP_RADIUS)

        icon_size = self._FROZEN_ICON_SIZE
        icon_x = rect.left() + self._CHIP_PAD_H
        icon_y = rect.top() + (rect.height() - icon_size) / 2
        pixmap = design.icon("pin", design.color("CHIP_DARK_FG")).pixmap(icon_size, icon_size)
        painter.drawPixmap(QPointF(icon_x, icon_y), pixmap)

        text_x = icon_x + icon_size + self._FROZEN_INNER_GAP
        baseline = rect.top() + (rect.height() - fm.height()) / 2 + fm.ascent()
        painter.setFont(font)
        painter.setPen(design.color("CHIP_DARK_FG"))
        painter.drawText(QPointF(text_x, baseline), self._FROZEN_LABEL)


class _MonitorVeil(QWidget):
    """SNX-58: the non-interactive Wayland companion `open_overlay` shows
    on every monitor besides the one the real, interactive `OverlayWindow`
    covers.

    A Wayland client cannot span two outputs with one surface -- a
    fullscreen request is inherently a single `wl_output`'s, per
    `OverlayWindow.show_on_screen`'s own docstring -- so covering a
    multi-monitor virtual desktop there takes one fullscreen surface per
    monitor rather than one big window the way X11's single `OverlayWindow`
    already can. This is deliberately not another `OverlayWindow`: only one
    monitor is ever the interactive one for a given snip, so the rest just
    need their own frozen, dimmed pixels and nothing else -- no bar, no
    tray, no selection of their own. It never seeks focus or reacts to
    input; `open_overlay` closes every instance of this the moment the
    real `OverlayWindow` does, via that window's own `on_dismissed`.
    """

    def __init__(self, monitor_frame: Frame, parent=None):
        super().__init__(parent)
        self._image = monitor_frame.image
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
        )
        # Sized to this monitor alone, exactly like `Overlay` above --
        # `show_on_screen` below is what fullscreens it onto the matching
        # real `QScreen`; this resize is what its own paintEvent's
        # `self.rect()` reads while unscreened (e.g. under the offscreen
        # platform tests run with).
        self.resize(
            round(monitor_frame.logical_size.width()),
            round(monitor_frame.logical_size.height()),
        )

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.drawImage(QRectF(self.rect()), self._image)
        # The same flat scrim `Overlay.VEIL_COLOR` dims an unselected
        # monitor with -- reused rather than re-typed, since this widget
        # never punches a selection hole in it the way `Overlay` does.
        painter.fillRect(QRectF(self.rect()), Overlay.VEIL_COLOR)
        painter.end()

    def show_on_screen(self, screen: QScreen | None) -> None:
        """Same contract as `OverlayWindow.show_on_screen` -- see its
        docstring; this window is only ever shown from the Wayland branch
        of `open_overlay`, so unlike that method there is no plain-`show()`
        case to fall back to other than a missing `screen` itself.
        """
        if screen is not None:
            self.winId()
            handle = self.windowHandle()
            if handle is not None:
                handle.setScreen(screen)
        self.showFullScreen()


def _screen_for_geometry(geometry: QRectF) -> QScreen | None:
    """The real `QScreen` whose own geometry matches `geometry` exactly, or
    None if none does -- e.g. under the offscreen platform tests run with,
    which reports no real screens a synthetic `monitor_geometries` entry
    could ever match. `OverlayWindow.show_on_screen`/`_MonitorVeil.
    show_on_screen` both already treat a None screen as "nothing more
    specific to target."
    """
    for screen in QApplication.screens():
        if QRectF(screen.geometry()) == geometry:
            return screen
    return None


def _interactive_geometry(monitor_geometries: list[QRectF]) -> QRectF:
    """Which monitor gets the one interactive window on Wayland.

    `monitor_geometries[0]` is whatever `QGuiApplication.screens()` happened
    to list first, which Qt does not promise is the primary screen -- on a
    three-monitor desktop that is a one-in-three chance of opening the only
    usable overlay on a monitor off to the side while the user watches the
    middle one do nothing. The primary screen is the deterministic answer,
    and the same one GNOME's own screenshot UI opens on.

    Not `QCursor.pos()`, tempting though "the monitor being pointed at" is:
    this file already refuses global cursor state on purpose -- see
    `_cursor_pos`, tracked from real move events precisely so no test has
    to control a system-wide pointer -- and `open_overlay` runs before any
    window exists to have seen a move. The old first-entry behaviour stays
    as the last resort so a caller passing synthetic geometries (every test
    of this function) still gets a deterministic answer.
    """
    if not monitor_geometries:
        return QRectF()
    primary = QApplication.primaryScreen()
    if primary is not None:
        geometry = QRectF(primary.geometry())
        if geometry in monitor_geometries:
            return geometry
    return monitor_geometries[0]


def open_overlay(
    frame: Frame,
    monitor_geometries: list[QRectF],
    *,
    wayland: bool,
    # SNX-65: off by default -- see `OverlayWindow.__init__`, whose own
    # `hints_enabled` this passes straight through.
    hints_enabled: bool = False,
    geometry_provider: GeometryProvider | None = None,
    registry: BackendRegistry | None = None,
    on_dismissed: Callable[[], None] | None = None,
    on_captured: "Callable[[QImage, Path | None], None] | None" = None,
    # rect, delay, and the chooser's after-capture destination ("instant" or
    # "save") -- see `OverlayWindow.__init__`'s own comment on the same
    # parameter.
    on_recording_requested: "Callable[[QRectF | None, str, str], None] | None" = None,
    # Enter, while a recording is armed. Fires the stage's primary action,
    # which on the record side is Record -- see `OverlayWindow`'s own
    # keyPressEvent, where the alternative was copying a screenshot of the
    # region a recording was being set up around.
    on_recording_start: "Callable[[], None] | None" = None,
) -> OverlayWindow:
    """Build and show the overlay for one snip, positioned for the
    caller's already-detected session type (`wayland`) rather than assumed
    here, per CLAUDE.md. Returns the single interactive `OverlayWindow` --
    the only widget a caller (`app.py`) needs to keep a reference to; any
    `_MonitorVeil` companions this creates are owned by a closure wired
    through `OverlayWindow`'s own `on_dismissed` and close themselves the
    moment the returned window does, so a caller's bookkeeping never has
    to know they exist.

    `on_dismissed` (SNX-62) is the caller's own hook for that same moment
    -- composed with the veil-closing closure below rather than handed to
    `OverlayWindow` in its place, so a caller (`AppController`, to drop its
    stale `_overlay` reference the instant the session actually ends)
    doesn't have to know whether this particular snip has veils to close at
    all.

    X11 (`wayland=False`): unchanged from before this ticket -- one
    `OverlayWindow` sized to the whole virtual desktop (every entry in
    `monitor_geometries`), shown via `show_on_screen(None)`, which is
    exactly the plain `setGeometry`-then-`show()` this window already did.

    Wayland: `OverlayWindow.show_on_screen`'s docstring is the authority
    for why a single window is fullscreened onto one specific `QScreen`
    instead. Fullscreen is inherently one output at a time, so with more
    than one monitor the interactive `OverlayWindow` is cropped
    (`Frame.crop`, the same helper `Overlay` above already uses) to just
    the first monitor, and a non-interactive `_MonitorVeil` -- cropped and
    fullscreened the same way -- covers each of the rest, so every
    window's own local (0, 0) lines up with the real screen pixels under
    it and none of them paint a stretched or offset copy of another
    monitor's content.
    """
    primary_geometry = (
        _interactive_geometry(monitor_geometries)
        if monitor_geometries
        else QRectF(frame.logical_origin, frame.logical_size)
    )

    veils: list[_MonitorVeil] = []

    def _on_overlay_dismissed() -> None:
        for veil in veils:
            veil.close()
        if on_dismissed is not None:
            on_dismissed()

    multi_monitor_wayland = wayland and len(monitor_geometries) > 1
    overlay_monitor_geometries = (
        [primary_geometry] if multi_monitor_wayland else monitor_geometries
    )
    # None (not the closure above) whenever neither half of it would do
    # anything -- no veils to close *and* no caller-supplied hook -- so
    # OverlayWindow._on_dismissed stays exactly None for a plain
    # single-window session, same as before this ticket's `on_dismissed`
    # parameter existed.
    needs_dismissal_hook = multi_monitor_wayland or on_dismissed is not None
    overlay = OverlayWindow(
        frame.crop(primary_geometry) if multi_monitor_wayland else frame,
        hints_enabled=hints_enabled,
        geometry_provider=geometry_provider,
        monitor_geometries=overlay_monitor_geometries,
        registry=registry,
        on_dismissed=_on_overlay_dismissed if needs_dismissal_hook else None,
        on_captured=on_captured,
        on_recording_requested=on_recording_requested,
        on_recording_start=on_recording_start,
    )

    if not wayland:
        overlay.show_on_screen(None)
        return overlay

    overlay.show_on_screen(_screen_for_geometry(primary_geometry))
    # Every monitor except the interactive one, by identity rather than by
    # slicing off the first entry: `_interactive_geometry` may well have
    # picked something other than `monitor_geometries[0]`, and a `[1:]`
    # slice would then leave the chosen monitor double-covered and one
    # other monitor bare.
    for geometry in monitor_geometries:
        if geometry == primary_geometry:
            continue
        veil = _MonitorVeil(frame.crop(geometry))
        veil.show_on_screen(_screen_for_geometry(geometry))
        veils.append(veil)
    return overlay


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
