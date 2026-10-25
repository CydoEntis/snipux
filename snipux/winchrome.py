"""Shared chrome for snipux's two ordinary windows -- Settings and review.

`docs/design/handoff-windows.md` is the authority for everything here. Both
windows are frameless and draw their own title bar, so they need the same
title bar, the same footer, the same switches and buttons; building that
twice is how the two drift apart. The overlay does not use any of it -- it
has no window chrome at all, and its palette is warm glass over a frozen
desktop rather than this opaque neutral dark.

Nothing in this module knows what a setting or a screenshot is. It is the
frame; `settings.py` and `review.py` fill it.
"""

from __future__ import annotations

from typing import Callable

from PyQt6.QtCore import QEvent, QPoint, QRect, QRectF, QSize, Qt, pyqtSignal
from PyQt6.QtGui import (
    QColor,
    QFont,
    QGuiApplication,
    QKeySequence,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QRadialGradient,
)
from PyQt6.QtWidgets import (
    QAbstractButton,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from . import design
from .design import tokens

# Every icon size is the same mark drawn at its own resolution -- see
# design/logo/generate_icons.py. The title bar mark below picks from the
# three small ones because that is the size it is shown at.
# How far in from an edge still counts as grabbing that edge. Matched to
# what a window manager gives its own borders -- much less and the window
# is not resizable in practice, much more and clicks near the edge of the
# content start missing what they were aimed at.
_RESIZE_MARGIN = 7

_RESIZE_CURSORS = {
    Qt.CursorShape.SizeHorCursor,
    Qt.CursorShape.SizeVerCursor,
    Qt.CursorShape.SizeFDiagCursor,
    Qt.CursorShape.SizeBDiagCursor,
}

_LOGO_DIR = design.PACKAGE_DIR / "design" / "logo"
_SMALL_MARK_SIZES = (16, 24, 32)
_TITLEBAR_MARK_SIZE = 16  # logical px


def _title_bar_mark_pixmap() -> QPixmap:
    """The snipux mark for the title bar, at the small artwork size closest
    to the screen's actual pixel density.

    Returns a null QPixmap if the artwork cannot be loaded -- a missing or
    unreadable `design/logo/` must not stop the window from opening, the
    same "a failure must not stop the rest" rule `app.py`'s
    `load_app_icon()` follows for the tray icon (SNX-81); the caller falls
    back to the plain accent square this used to always be.
    """
    screen = QGuiApplication.primaryScreen()
    dpr = screen.devicePixelRatio() if screen is not None else 1.0
    physical = round(_TITLEBAR_MARK_SIZE * dpr)

    size = next((s for s in _SMALL_MARK_SIZES if s >= physical), _SMALL_MARK_SIZES[-1])
    pixmap = QPixmap(str(_LOGO_DIR / f"snipux-{size}.png"))
    if pixmap.isNull():
        return pixmap

    if pixmap.width() != physical:
        pixmap = pixmap.scaled(
            physical,
            physical,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
    pixmap.setDevicePixelRatio(dpr)
    return pixmap


def _ui_font(size: float, weight: int) -> QFont:
    font = QFont(design.font_families().ui)
    font.setPixelSize(round(size))
    font.setWeight(QFont.Weight(weight))
    return font


def _mono_font(size: float, weight: int = 400) -> QFont:
    font = QFont(design.font_families().mono)
    font.setPixelSize(round(size))
    font.setWeight(QFont.Weight(weight))
    return font


class Switch(QAbstractButton):
    """The 34 x 19 toggle used throughout Settings.

    A checkbox with a stylesheet cannot reach this shape, and the design
    uses it in enough places -- After capture, Saving, Annotation, all four
    Tray & startup rows -- that painting it once is cheaper than fighting
    QCheckBox eight times.
    """

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(tokens.WinMetric.SWITCH_W, tokens.WinMetric.SWITCH_H)

    def sizeHint(self) -> QSize:
        return QSize(tokens.WinMetric.SWITCH_W, tokens.WinMetric.SWITCH_H)

    def paintEvent(self, event) -> None:
        metric = tokens.WinMetric
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        track = QRectF(0, 0, self.width(), self.height())
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(
            QColor(tokens.Color.ACCENT if self.isChecked() else tokens.Win.TOGGLE_OFF)
        )
        painter.drawRoundedRect(track, self.height() / 2, self.height() / 2)

        knob = metric.SWITCH_KNOB
        x = (
            self.width() - metric.SWITCH_PAD - knob
            if self.isChecked()
            else metric.SWITCH_PAD
        )
        painter.setBrush(QColor(tokens.Win.TOGGLE_KNOB))
        painter.drawEllipse(QRectF(x, metric.SWITCH_PAD, knob, knob))
        painter.end()


class SecondaryButton(QPushButton):
    """The neutral button: Cancel, Choose..., Show in Folder, Save As..."""

    def __init__(self, text: str, parent: QWidget | None = None):
        super().__init__(text, parent)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFont(_ui_font(12.5, 500))
        self.setFixedHeight(tokens.WinMetric.FOOTER_BTN_H)
        win, metric = tokens.Win, tokens.WinMetric
        self.setStyleSheet(
            f"QPushButton {{ background: {win.CONTROL_BG};"
            f" border: 1px solid {win.CONTROL_BORDER};"
            f" border-radius: {metric.CONTROL_RADIUS}px;"
            f" color: {win.TEXT_SECONDARY}; padding: 0 14px; }}"
            f"QPushButton:hover {{ background: {win.CONTROL_BG_HOVER};"
            f" border-color: {win.CONTROL_BORDER_HOVER}; }}"
            f"QPushButton:disabled {{ color: {win.TEXT_DISABLED}; }}"
        )


class AccentButton(QPushButton):
    """The one primary action per window -- Save in Settings, Copy in review."""

    def __init__(self, text: str, parent: QWidget | None = None):
        super().__init__(text, parent)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFont(_ui_font(12.5, 600))
        self.setFixedHeight(tokens.WinMetric.FOOTER_BTN_H)
        metric = tokens.WinMetric
        self.setStyleSheet(
            f"QPushButton {{ background: {tokens.Color.ACCENT}; border: none;"
            f" border-radius: {metric.CONTROL_RADIUS}px;"
            f" color: {tokens.Color.ACCENT_FG}; padding: 0 16px; }}"
            f"QPushButton:disabled {{ background: {tokens.Win.CONTROL_BG};"
            f" color: {tokens.Win.TEXT_DISABLED}; }}"
        )


class SectionHeading(QLabel):
    """The uppercase rule above a group of settings."""

    def __init__(self, text: str, parent: QWidget | None = None):
        super().__init__(text.upper(), parent)
        font = _ui_font(10.5, 600)
        font.setLetterSpacing(QFont.SpacingType.PercentageSpacing, 110)
        self.setFont(font)
        self.setStyleSheet(f"color: {tokens.Win.TEXT_SECTION};")


def open_filter(label: str, suffixes: tuple[str, ...]) -> str:
    """A file dialog filter such as `Images (*.png *.jpg)`."""
    return f"{label} ({' '.join('*' + suffix for suffix in suffixes)})"


class _DropZone(QWidget):
    """The card in an empty editor: a dashed outline that turns accent, over
    a faint accent wash, while a file hovers over the window.
    """

    RADIUS = 14

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.active = False

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        if self.active:
            wash = QColor(tokens.Color.ACCENT)
            wash.setAlphaF(0.06)
            edge = QColor(tokens.Color.ACCENT)
        else:
            wash = QColor(tokens.Win.CHROME_BG)
            wash.setAlphaF(0.55)
            edge = QColor(tokens.Win.CONTROL_BORDER_HOVER)
        painter.setBrush(wash)
        pen = QPen(edge, 1.5)
        pen.setDashPattern([5, 4])
        painter.setPen(pen)
        painter.drawRoundedRect(rect, self.RADIUS, self.RADIUS)
        painter.end()


class EmptyState(QWidget):
    """What an editor shows with no file open: what it is for, Open…, and
    a place to drop a file.

    It stands where the canvas would, on the same workspace gradient, so
    opening a file swaps one for the other rather than changing the
    window's shape. The window, not this widget, accepts the drop -- a
    file let go anywhere over it should open -- and tells this widget
    while one is hovering, so the drop zone can light up.
    """

    openRequested = pyqtSignal()

    _CARD_W = 400
    _ICON = 30
    _ICON_WELL = 60

    def __init__(self, icon_name: str, heading: str, line: str, parent: QWidget | None = None):
        super().__init__(parent)
        win = tokens.Win

        outer = QVBoxLayout(self)
        outer.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.card = _DropZone()
        self.card.setFixedWidth(self._CARD_W)
        outer.addWidget(self.card, 0, Qt.AlignmentFlag.AlignCenter)

        column = QVBoxLayout(self.card)
        column.setContentsMargins(32, 34, 32, 30)
        column.setSpacing(0)
        column.setAlignment(Qt.AlignmentFlag.AlignHCenter)

        well = QLabel()
        well.setFixedSize(self._ICON_WELL, self._ICON_WELL)
        well.setAlignment(Qt.AlignmentFlag.AlignCenter)
        well.setPixmap(
            design.icon(icon_name, win.ICON_IDLE).pixmap(QSize(self._ICON, self._ICON))
        )
        well.setStyleSheet(
            f"background: {win.CONTROL_BG}; border: 1px solid {win.CONTROL_BORDER};"
            f" border-radius: {self._ICON_WELL // 2}px;"
        )
        column.addWidget(well, 0, Qt.AlignmentFlag.AlignHCenter)
        column.addSpacing(18)

        self.heading = self._label(heading, _ui_font(15, 600), win.TEXT_PRIMARY)
        column.addWidget(self.heading)
        column.addSpacing(6)
        self.line = self._label(line, _ui_font(12.5, 400), win.TEXT_MUTED)
        column.addWidget(self.line)
        column.addSpacing(22)

        self.open_button = AccentButton("Open…")
        self.open_button.clicked.connect(self.openRequested)
        column.addWidget(self.open_button, 0, Qt.AlignmentFlag.AlignHCenter)
        column.addSpacing(12)

        shortcut = QKeySequence(QKeySequence.StandardKey.Open).toString(
            QKeySequence.SequenceFormat.NativeText
        )
        self.hint = self._label(
            f"or drop a file here  ·  {shortcut}", _ui_font(11.5, 400), win.TEXT_FAINT
        )
        column.addWidget(self.hint)

    @staticmethod
    def _label(text: str, font: QFont, colour: str) -> QLabel:
        label = QLabel(text)
        label.setFont(font)
        label.setWordWrap(True)
        label.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        label.setStyleSheet(f"color: {colour}; background: transparent;")
        return label

    @property
    def drag_active(self) -> bool:
        return self.card.active

    def set_drag_active(self, active: bool) -> None:
        self.card.active = active
        self.card.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        _kind, (cx, cy), radius, stops = tokens.Gradient.WORKSPACE
        gradient = QRadialGradient(self.width() * cx, self.height() * cy, self.width() * radius)
        for position, colour in stops:
            gradient.setColorAt(position, QColor(colour))
        painter.fillRect(self.rect(), gradient)
        painter.end()


class _TitleBarButton(QPushButton):
    """Minimise / maximise / close. Close alone gets a red hover, the way
    every GNOME title bar behaves.
    """

    def __init__(self, icon_name: str, danger: bool = False, parent=None):
        super().__init__(parent)
        metric = tokens.WinMetric
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(metric.TITLEBAR_BTN, metric.TITLEBAR_BTN)
        self.set_glyph(icon_name)
        self.setIconSize(QSize(metric.TITLEBAR_ICON, metric.TITLEBAR_ICON))
        hover = tokens.Win.CLOSE_HOVER if danger else tokens.Win.ROW_HOVER
        self.setStyleSheet(
            f"QPushButton {{ background: transparent; border: none;"
            f" border-radius: {metric.TITLEBAR_BTN_R}px; }}"
            f"QPushButton:hover {{ background: {hover}; }}"
        )

    def set_glyph(self, icon_name: str) -> None:
        self.glyph = icon_name
        self.setIcon(design.icon(icon_name, tokens.Win.TITLEBAR_ICON))


# Windows' own scrollbar is 17px of grey chrome with two arrow buttons and,
# on a themed desktop, the system accent colour -- which is how a lime-green
# scrollbar appeared down the side of Settings. Qt cannot restyle the native
# one; a stylesheet replaces it outright, so this is the only way for a
# scrollbar to look like it belongs to this app.
#
# No arrow buttons (nothing has clicked them since about 2010), a track that
# is simply the window behind it, and a handle with the same weight as a
# control border.
SCROLLBAR_W = 10
_SCROLLBAR_RADIUS = SCROLLBAR_W // 2
_SCROLLBAR_MIN = 28


def scrollbar_style() -> str:
    """The app's scrollbar, as a stylesheet. Applied by `WinWindow` to every
    window, and by anything that sets its own stylesheet on a widget that
    scrolls -- a widget's own sheet takes precedence over its window's."""
    win = tokens.Win
    return f"""
        QScrollBar:vertical {{ background: transparent; width: {SCROLLBAR_W}px;
            margin: 2px 2px 2px 0; }}
        QScrollBar:horizontal {{ background: transparent; height: {SCROLLBAR_W}px;
            margin: 0 2px 2px 2px; }}
        QScrollBar::handle:vertical {{ background: {win.SCROLL_THUMB};
            border-radius: {_SCROLLBAR_RADIUS}px; min-height: {_SCROLLBAR_MIN}px; }}
        QScrollBar::handle:horizontal {{ background: {win.SCROLL_THUMB};
            border-radius: {_SCROLLBAR_RADIUS}px; min-width: {_SCROLLBAR_MIN}px; }}
        QScrollBar::handle:hover {{ background: {win.SCROLL_THUMB_HOVER}; }}
        QScrollBar::handle:pressed {{ background: {win.SCROLL_THUMB_ACTIVE}; }}
        QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
        QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
    """


class WinWindow(QWidget):
    """A frameless window with snipux's own title bar and an optional footer.

    Frameless because the design draws its own chrome; that costs us the
    window manager's drag and its buttons, so both are reimplemented here --
    `title_bar` is draggable, and the three buttons do what their glyphs
    say.

    Subclasses fill `body` (between the title bar and the footer) and
    `footer_left` / `footer_right`. `footer_height` of 0 omits the footer
    entirely, which the review window's own footer layout does not want but
    a future window might.
    """

    closed = pyqtSignal()

    def __init__(
        self,
        title: str,
        *,
        size: tuple[int, int],
        parent: QWidget | None = None,
        show_footer: bool = True,
    ):
        super().__init__(parent)
        metric = tokens.WinMetric
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Window)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.resize(*size)
        # A floor, so a window cannot be dragged down to a sliver it can
        # never be got back out of. Subclasses with a real minimum (the
        # player's 980x640) set their own over the top of this.
        self.setMinimumSize(420, 300)
        self._drag_origin: QPoint | None = None
        self.setStyleSheet(scrollbar_style())

        # Frameless means the window manager gives us no resize borders, so
        # we grow our own. The edges are covered by child widgets -- the
        # title bar owns the top, the footer the bottom, the body the sides
        # -- so this window never sees a mouse event there on its own. An
        # event filter on every descendant is what puts the edges back
        # within reach.
        self._resize_edges = Qt.Edge(0)
        self._resize_from: tuple[QRect, QPoint] | None = None
        self.setMouseTracking(True)
        self._watch_for_edges(self)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self.title_bar = self._build_title_bar(title)
        outer.addWidget(self.title_bar)

        self.body = QWidget()
        self.body.setStyleSheet("background: transparent;")
        outer.addWidget(self.body, 1)

        self.footer: QWidget | None = None
        if show_footer:
            self.footer = self._build_footer()
            outer.addWidget(self.footer)

    # -- chrome ---------------------------------------------------------

    def _build_title_bar(self, title: str) -> QWidget:
        metric = tokens.WinMetric
        bar = QWidget()
        bar.setFixedHeight(metric.TITLEBAR_H)
        bar.setStyleSheet("background: transparent;")
        row = QHBoxLayout(bar)
        row.setContentsMargins(13, 0, 8, 0)
        row.setSpacing(9)

        # The app mark, drawn from SNX-102's small-size artwork rather than
        # the plain accent square this used to be (SNX-106) -- see
        # _title_bar_mark_pixmap(). A null pixmap (artwork missing/broken)
        # falls back to that square rather than leaving the label blank.
        mark = QLabel()
        mark.setFixedSize(_TITLEBAR_MARK_SIZE, _TITLEBAR_MARK_SIZE)
        pixmap = _title_bar_mark_pixmap()
        if pixmap.isNull():
            mark.setFixedSize(10, 10)
            mark.setStyleSheet(f"background: {tokens.Color.ACCENT}; border-radius: 3px;")
        else:
            mark.setPixmap(pixmap)
        self.title_mark = mark
        row.addWidget(mark)

        self.title_label = QLabel(title)
        self.title_label.setFont(_ui_font(12.5, 500))
        self.title_label.setStyleSheet(f"color: {tokens.Win.TEXT_TITLE};")
        row.addWidget(self.title_label)

        self.title_detail = QLabel()
        self.title_detail.setFont(_mono_font(11))
        self.title_detail.setStyleSheet(f"color: {tokens.Win.TEXT_FAINT};")
        row.addWidget(self.title_detail)

        row.addStretch()

        self.minimise_button = _TitleBarButton("minimize")
        self.minimise_button.clicked.connect(self.showMinimized)
        row.addWidget(self.minimise_button)

        self.maximise_button = _TitleBarButton("maximize")
        self.maximise_button.setToolTip("Maximize")
        self.maximise_button.clicked.connect(self._toggle_maximised)
        row.addWidget(self.maximise_button)

        self.close_button = _TitleBarButton("close", danger=True)
        self.close_button.clicked.connect(self.close)
        row.addWidget(self.close_button)
        return bar

    def _build_footer(self) -> QWidget:
        metric = tokens.WinMetric
        footer = QWidget()
        footer.setFixedHeight(metric.FOOTER_H)
        footer.setStyleSheet("background: transparent;")
        row = QHBoxLayout(footer)
        row.setContentsMargins(metric.REVIEW_FOOTER_PAD[1], 0, metric.REVIEW_FOOTER_PAD[1], 0)
        row.setSpacing(9)
        self.footer_left = QHBoxLayout()
        self.footer_left.setSpacing(9)
        self.footer_right = QHBoxLayout()
        self.footer_right.setSpacing(9)
        row.addLayout(self.footer_left)
        row.addStretch()
        row.addLayout(self.footer_right)
        return footer

    # -- resizing --------------------------------------------------------

    def _watch_for_edges(self, widget: QWidget) -> None:
        """Filter `widget` and everything under it, now and later.

        `ChildAdded` is filtered too, so a widget built after construction
        -- which is most of them, since subclasses fill `body` afterwards --
        is picked up without anyone having to remember to register it.
        """
        widget.installEventFilter(self)
        widget.setMouseTracking(True)
        for child in widget.findChildren(QWidget):
            child.installEventFilter(self)
            child.setMouseTracking(True)

    def _edges_at(self, point: QPoint) -> Qt.Edge:
        """Which window edges `point` (in this window's coordinates) grabs."""
        edges = Qt.Edge(0)
        if point.x() <= _RESIZE_MARGIN:
            edges |= Qt.Edge.LeftEdge
        elif point.x() >= self.width() - _RESIZE_MARGIN:
            edges |= Qt.Edge.RightEdge
        if point.y() <= _RESIZE_MARGIN:
            edges |= Qt.Edge.TopEdge
        elif point.y() >= self.height() - _RESIZE_MARGIN:
            edges |= Qt.Edge.BottomEdge
        return edges

    @staticmethod
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

    def eventFilter(self, watched, event) -> bool:
        kind = event.type()
        if kind == QEvent.Type.ChildAdded:
            child = event.child()
            if isinstance(child, QWidget):
                self._watch_for_edges(child)
        elif kind in (
            QEvent.Type.MouseMove,
            QEvent.Type.MouseButtonPress,
            QEvent.Type.MouseButtonRelease,
        ):
            if self._handle_edge_event(kind, event):
                return True
        return super().eventFilter(watched, event)

    def _handle_edge_event(self, kind, event) -> bool:
        """True when the event was a resize gesture and must go no further.

        Returning False for everything else matters as much as returning
        True here: these events are filtered on *every* descendant, so
        swallowing one that was not a resize would break every button in
        the window.
        """
        if self.isMaximized() or self.isFullScreen():
            return False
        try:
            global_point = event.globalPosition().toPoint()
        except AttributeError:
            return False
        local = self.mapFromGlobal(global_point)

        if kind == QEvent.Type.MouseButtonRelease:
            if self._resize_from is None:
                return False
            self._resize_from = None
            self.unsetCursor()
            return True

        if kind == QEvent.Type.MouseMove:
            if self._resize_from is not None:
                self._resize_to(global_point)
                return True
            edges = self._edges_at(local)
            if edges:
                self.setCursor(self._cursor_for(edges))
                return True
            # Only ours to unset: a child that set its own cursor (the
            # rail's pointing hand, a text field's I-beam) must keep it.
            if self.cursor().shape() in _RESIZE_CURSORS:
                self.unsetCursor()
            return False

        if event.button() != Qt.MouseButton.LeftButton:
            return False
        edges = self._edges_at(local)
        if not edges:
            return False

        # Ask the compositor first. It knows about snapping, about screen
        # edges, and it is the only thing that can resize a window under
        # Wayland at all; the manual path below is the fallback, and the
        # one the offscreen platform in the tests uses.
        handle = self.windowHandle()
        if handle is not None and handle.startSystemResize(edges):
            return True
        self._resize_edges = edges
        self._resize_from = (self.geometry(), global_point)
        return True

    def _resize_to(self, global_point: QPoint) -> None:
        """The manual fallback: work out the new geometry ourselves."""
        if self._resize_from is None:
            return
        start_geometry, start_point = self._resize_from
        delta = global_point - start_point
        rect = QRect(start_geometry)
        minimum = self.minimumSize()

        # `right`/`bottom` are the last pixel INSIDE the rect, so a width of
        # `w` spans left..left + w - 1. Without the -1 the window clamps to
        # one pixel over its own minimum, which is the kind of thing nobody
        # sees and every geometry test does.
        if self._resize_edges & Qt.Edge.LeftEdge:
            rect.setLeft(min(rect.left() + delta.x(), rect.right() - minimum.width() + 1))
        elif self._resize_edges & Qt.Edge.RightEdge:
            rect.setRight(max(rect.right() + delta.x(), rect.left() + minimum.width() - 1))
        if self._resize_edges & Qt.Edge.TopEdge:
            rect.setTop(min(rect.top() + delta.y(), rect.bottom() - minimum.height() + 1))
        elif self._resize_edges & Qt.Edge.BottomEdge:
            rect.setBottom(max(rect.bottom() + delta.y(), rect.top() + minimum.height() - 1))
        self.setGeometry(rect)

    def _toggle_maximised(self) -> None:
        if self.isMaximized():
            self.showNormal()
        else:
            self.showMaximized()

    def changeEvent(self, event) -> None:
        # Driven by the state change rather than the click, so maximising
        # by a double-click, Win+Up or a snap also swaps the glyph.
        if event.type() == QEvent.Type.WindowStateChange:
            maximised = self.isMaximized()
            self.maximise_button.set_glyph("restore" if maximised else "maximize")
            self.maximise_button.setToolTip("Restore" if maximised else "Maximize")
        super().changeEvent(event)

    # -- frameless drag --------------------------------------------------
    #
    # A frameless window gets none of the window manager's move handling, so
    # the title bar has to do it. Tracked from the press position rather than
    # by deltas so a fast drag cannot accumulate rounding error.

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if (
            event.button() == Qt.MouseButton.LeftButton
            # The outer few pixels of the title bar are a resize edge, not
            # a drag handle -- checked first, or the top corners could only
            # ever move the window and never resize it.
            and not self._edges_at(event.position().toPoint())
            and event.position().y() <= tokens.WinMetric.TITLEBAR_H
        ):
            self._drag_origin = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._drag_origin is not None and not self.isMaximized():
            self.move(event.globalPosition().toPoint() - self._drag_origin)
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        self._drag_origin = None
        super().mouseReleaseEvent(event)

    # -- painting --------------------------------------------------------

    def paintEvent(self, event) -> None:
        """The window body, its outline, and the two chrome bands.

        Painted rather than styled: the 12px radius has to clip the title
        bar and footer fills as well as the body, and a stylesheet on a
        translucent frameless widget leaves square corners behind the
        rounded ones.
        """
        metric, win = tokens.WinMetric, tokens.Win
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        path = QPainterPath()
        path.addRoundedRect(rect, metric.WINDOW_RADIUS, metric.WINDOW_RADIUS)
        painter.setClipPath(path)

        painter.fillRect(self.rect(), QColor(win.WINDOW_BG))

        chrome = QColor(win.CHROME_BG)
        painter.fillRect(0, 0, self.width(), metric.TITLEBAR_H, chrome)
        if self.footer is not None:
            painter.fillRect(
                0,
                self.height() - metric.FOOTER_H,
                self.width(),
                metric.FOOTER_H,
                chrome,
            )

        painter.setPen(QColor(win.SEPARATOR))
        painter.drawLine(0, metric.TITLEBAR_H, self.width(), metric.TITLEBAR_H)
        if self.footer is not None:
            y = self.height() - metric.FOOTER_H
            painter.drawLine(0, y, self.width(), y)

        painter.setClipping(False)
        painter.setPen(QColor(win.BORDER))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(rect, metric.WINDOW_RADIUS, metric.WINDOW_RADIUS)
        painter.end()

    def closeEvent(self, event) -> None:
        self.closed.emit()
        super().closeEvent(event)
