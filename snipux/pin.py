"""SNX-83: Pin -- a snip kept on top of everything else, exactly where it
was cut, so it can be worked from while the user is in another window.

`OverlayWindow._on_bar_pin` hands this module a rendered image and the
selection's own absolute screen rect; `AppController._on_pin_requested`
builds and shows a `PinWindow` from them and keeps it in a list for the
same GC-survival reason `_reviews`/`_players` are. Nothing here decides
*whether* a pin can be offered -- that is `platform.Platform.can_pin()`,
asked once, at the destination menu (`overlay._open_destination_menu`),
never here: by the time this constructor runs, pinning has already been
allowed.

Not built on `WinWindow` (`winchrome.py`): that chrome's entire shape is a
title bar and an optional footer, and a pin has neither -- per the
acceptance criteria it is dragged by grabbing the image itself and closed
by Escape or a close affordance that only appears on hover, so there is no
row of chrome to reuse. What *is* borrowed is the order `WinWindow`'s own
resize borders already established: ask the compositor first
(`QWindow.startSystemMove`/`startSystemResize`), and only move or resize
the widget by hand when that refuses -- true under a Qt platform with no
native window server behind it (the offscreen platform the test suite
runs on), where `windowHandle()` is None until the widget is actually shown.

CLAUDE.md's one rule is why a pin's own presence on screen is not a bug:
the app grabs the whole virtual desktop in a single shot, so a pin left up
is genuinely part of the desktop the next snip sees.
`Platform.exclude_from_capture` exists for chrome that must not film
itself; nothing here calls it, on purpose.
"""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import QPoint, QRect, QSize, Qt, pyqtSignal
from PyQt6.QtGui import QCursor, QImage, QMouseEvent, QPainter
from PyQt6.QtWidgets import QMenu, QPushButton, QToolTip, QWidget

from . import output, setup_desktop
from .design import tokens

# How far in from an edge still counts as grabbing it, matched to
# `winchrome._RESIZE_MARGIN` -- much less and the window is not resizable
# in practice, much more and clicks near the image's own edge start missing.
_RESIZE_MARGIN = 7

_CLOSE_SIZE = tokens.WinMetric.TITLEBAR_BTN
_CLOSE_MARGIN = 6
# The narrowest edge a pin can be shrunk to -- small enough that "smaller
# than the thing it was cut from" (the acceptance criterion) is never
# blocked by this, large enough that the close affordance still fits on it.
_MIN_EDGE = 48


def _edges_at(widget: QWidget, point: QPoint) -> Qt.Edge:
    edges = Qt.Edge(0)
    if point.x() <= _RESIZE_MARGIN:
        edges |= Qt.Edge.LeftEdge
    elif point.x() >= widget.width() - _RESIZE_MARGIN:
        edges |= Qt.Edge.RightEdge
    if point.y() <= _RESIZE_MARGIN:
        edges |= Qt.Edge.TopEdge
    elif point.y() >= widget.height() - _RESIZE_MARGIN:
        edges |= Qt.Edge.BottomEdge
    return edges


def _cursor_for(edges: Qt.Edge) -> Qt.CursorShape:
    left = bool(edges & Qt.Edge.LeftEdge)
    right = bool(edges & Qt.Edge.RightEdge)
    top = bool(edges & Qt.Edge.TopEdge)
    bottom = bool(edges & Qt.Edge.BottomEdge)
    if (left and top) or (right and bottom):
        return Qt.CursorShape.SizeFDiagCursor
    if (right and top) or (left and bottom):
        return Qt.CursorShape.SizeBDiagCursor
    if left or right:
        return Qt.CursorShape.SizeHorCursor
    return Qt.CursorShape.SizeVerCursor


class _CloseAffordance(QPushButton):
    """The corner control Pin substitutes for a title bar's close button --
    shown only on hover (`PinWindow.enterEvent`/`leaveEvent`), since there is
    no title bar for it to live in permanently."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(_CLOSE_SIZE, _CLOSE_SIZE)
        from .design import icon

        self.setIcon(icon("close", tokens.Win.TITLEBAR_ICON))
        self.setStyleSheet(
            f"QPushButton {{ background: rgba(18, 20, 24, 0.72); border: none;"
            f" border-radius: {tokens.WinMetric.TITLEBAR_BTN_R}px; }}"
            f"QPushButton:hover {{ background: {tokens.Win.CLOSE_HOVER}; }}"
        )
        self.hide()


class PinWindow(QWidget):
    """A captured selection, held on top of everything else until closed.

    Opened at exactly the rect the selection came from
    (`OverlayWindow._to_absolute_rect`), so that closing the overlay looks
    like the snipped area simply stayed behind -- landing it anywhere
    else (centred, cascaded) breaks that illusion, which is the detail the
    ticket calls out as what makes Pin feel right rather than gimmicky.
    """

    closed = pyqtSignal()

    def __init__(self, image: QImage, rect: QRect, parent: QWidget | None = None):
        super().__init__(parent)
        self._image = image
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            # `Tool`, the same reason `flowbars.py`'s HUDs need it
            # (TODO.md, "the capture flow"): a pin can be created and shown
            # before the fullscreen overlay it was cut from has finished
            # closing, and only this flag stacks a frameless window above
            # one that is itself always-on-top.
            | Qt.WindowType.Tool
        )
        # A utility window, not a document one: appearing must not steal
        # keyboard focus from whatever the user was already doing, which is
        # the entire point of pinning something to look at while working
        # elsewhere.
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setMouseTracking(True)

        # Built before `setGeometry` below, which fires `resizeEvent`
        # synchronously and reads `_resize_edges` and `_close_button`.
        self._drag_origin: QPoint | None = None
        self._resize_edges = Qt.Edge(0)
        self._resize_from: tuple[QRect, QPoint] | None = None
        self._close_button = _CloseAffordance(self)
        self._close_button.clicked.connect(self.close)

        aspect = image.width() / image.height()
        minimum = _min_size(aspect)
        # Never larger than the rect this pin is about to open at: a
        # minimum wider than a genuinely tiny selection would make Qt clamp
        # `setGeometry(rect)` below, breaking "opens at exactly the
        # selection's size" for the one case it would matter least to.
        self.setMinimumSize(
            min(minimum.width(), rect.width()), min(minimum.height(), rect.height())
        )
        # False for the `resizeEvent` that `setGeometry` below fires
        # synchronously: the caller's `rect` is authoritative on
        # construction and must land exactly, never nudged by the aspect
        # clamp on the strength of a rounding difference between it and
        # `image`'s own pixel size -- see `resizeEvent`.
        self._settled = False
        self.setGeometry(rect)
        self._settled = True

    # -- context menu (Copy / Save, so a pin is not a dead end) ----------

    def contextMenuEvent(self, event) -> None:
        self._build_context_menu().exec(event.globalPos())

    def _build_context_menu(self) -> QMenu:
        """Split from `contextMenuEvent` so a test can trigger `Copy`/`Save`
        without going through `QMenu.exec`, which blocks on a real event
        loop waiting for a click that offscreen tests never make.
        """
        menu = QMenu(self)
        menu.addAction("Copy", self.copy)
        menu.addAction("Save", self.save)
        return menu

    def copy(self) -> None:
        output.copy_image_to_clipboard(self._image)

    def save(self) -> Path | None:
        """Write the pinned image into the configured save folder, and
        return where it went -- or None when the write failed.

        `load_save_folder()` rather than a hardcoded `~/Pictures/snipux`:
        the two agree until someone changes it in Settings, and a pin that
        saved somewhere other than every other Save in the app was a
        surprise with no reason behind it.
        """
        try:
            return output.save_image(self._image, setup_desktop.load_save_folder())
        except OSError as exc:
            # No status line and no toast in this window -- it is a bare
            # pinned image -- so the one place a failure can be said is the
            # tooltip the pointer is already near.
            QToolTip.showText(QCursor.pos(), f"Could not save: {exc}", self)
            return None

    # -- hover close affordance -------------------------------------------

    def enterEvent(self, event) -> None:
        self._close_button.show()
        self._close_button.raise_()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._close_button.hide()
        super().leaveEvent(event)

    def _place_close_button(self) -> None:
        self._close_button.move(
            self.width() - _CLOSE_SIZE - _CLOSE_MARGIN, _CLOSE_MARGIN
        )

    # -- escape closes ------------------------------------------------------

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.close()
            return
        super().keyPressEvent(event)

    # -- drag by the image, resize by its edges ---------------------------

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return
        edges = _edges_at(self, event.position().toPoint())
        if edges:
            self._resize_edges = edges
            handle = self.windowHandle()
            if handle is not None and handle.startSystemResize(edges):
                return
            self._resize_from = (self.geometry(), event.globalPosition().toPoint())
            return
        handle = self.windowHandle()
        if handle is not None and handle.startSystemMove():
            return
        self._drag_origin = event.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._resize_from is not None:
            self._resize_to(event.globalPosition().toPoint())
            return
        if self._drag_origin is not None:
            self.move(event.globalPosition().toPoint() - self._drag_origin)
            return
        edges = _edges_at(self, event.position().toPoint())
        self.setCursor(_cursor_for(edges) if edges else Qt.CursorShape.SizeAllCursor)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        self._drag_origin = None
        self._resize_from = None
        super().mouseReleaseEvent(event)

    def _resize_to(self, global_point: QPoint) -> None:
        """The manual fallback: work out the new geometry ourselves, the
        same shape `WinWindow._resize_to` computes it in. Aspect ratio is
        not enforced here -- `resizeEvent` clamps every geometry change
        this produces, system-resize included, back onto the image's own
        ratio, so there is exactly one place that math has to be right.
        """
        if self._resize_from is None:
            return
        start_geometry, start_point = self._resize_from
        delta = global_point - start_point
        rect = QRect(start_geometry)
        minimum = self.minimumSize()
        if self._resize_edges & Qt.Edge.LeftEdge:
            rect.setLeft(min(rect.left() + delta.x(), rect.right() - minimum.width() + 1))
        elif self._resize_edges & Qt.Edge.RightEdge:
            rect.setRight(max(rect.right() + delta.x(), rect.left() + minimum.width() - 1))
        if self._resize_edges & Qt.Edge.TopEdge:
            rect.setTop(min(rect.top() + delta.y(), rect.bottom() - minimum.height() + 1))
        elif self._resize_edges & Qt.Edge.BottomEdge:
            rect.setBottom(max(rect.bottom() + delta.y(), rect.top() + minimum.height() - 1))
        self.setGeometry(rect)

    def resizeEvent(self, event) -> None:
        self._place_close_button()
        if not self._settled:
            super().resizeEvent(event)
            return
        clamped = self._aspect_clamped(self.size())
        if clamped != self.size():
            rect = self.geometry()
            if self._resize_edges & Qt.Edge.LeftEdge:
                rect.setLeft(rect.right() - clamped.width() + 1)
            else:
                rect.setWidth(clamped.width())
            if self._resize_edges & Qt.Edge.TopEdge:
                rect.setTop(rect.bottom() - clamped.height() + 1)
            else:
                rect.setHeight(clamped.height())
            self.setGeometry(rect)
            return
        super().resizeEvent(event)

    def _aspect_clamped(self, size: QSize) -> QSize:
        """`size`, adjusted onto the image's own aspect ratio.

        Height-driven only when the drag holds a top or bottom edge and
        neither a left nor a right one -- the one shape of drag where
        letting width drive would fight the direction the user is actually
        pulling. Every other case, corners included, is width-driven.
        """
        aspect = self._image.width() / self._image.height()
        vertical_only = self._resize_edges & (
            Qt.Edge.TopEdge | Qt.Edge.BottomEdge
        ) and not self._resize_edges & (Qt.Edge.LeftEdge | Qt.Edge.RightEdge)
        if vertical_only:
            height = size.height()
            return QSize(round(height * aspect), height)
        width = size.width()
        return QSize(width, round(width / aspect))

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.drawImage(self.rect(), self._image)
        painter.end()

    def closeEvent(self, event) -> None:
        self.closed.emit()
        super().closeEvent(event)


def _min_size(aspect: float) -> QSize:
    if aspect >= 1:
        return QSize(_MIN_EDGE, round(_MIN_EDGE / aspect))
    return QSize(round(_MIN_EDGE * aspect), _MIN_EDGE)
