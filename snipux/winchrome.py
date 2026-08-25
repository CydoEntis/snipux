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

from PyQt6.QtCore import QPoint, QRectF, QSize, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QMouseEvent, QPainter, QPainterPath
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


class _TitleBarButton(QPushButton):
    """Minimise / maximise / close. Close alone gets a red hover, the way
    every GNOME title bar behaves.
    """

    def __init__(self, icon_name: str, danger: bool = False, parent=None):
        super().__init__(parent)
        metric = tokens.WinMetric
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(metric.TITLEBAR_BTN, metric.TITLEBAR_BTN)
        self.setIcon(design.icon(icon_name, tokens.Win.TITLEBAR_ICON))
        self.setIconSize(QSize(metric.TITLEBAR_ICON, metric.TITLEBAR_ICON))
        hover = tokens.Win.CLOSE_HOVER if danger else tokens.Win.ROW_HOVER
        self.setStyleSheet(
            f"QPushButton {{ background: transparent; border: none;"
            f" border-radius: {metric.TITLEBAR_BTN_R}px; }}"
            f"QPushButton:hover {{ background: {hover}; }}"
        )


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
        self._drag_origin: QPoint | None = None

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

        # The accent square is the app mark -- a 10px block rather than the
        # full logo, which at 42px tall would be illegible anyway.
        mark = QLabel()
        mark.setFixedSize(10, 10)
        mark.setStyleSheet(f"background: {tokens.Color.ACCENT}; border-radius: 3px;")
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

        self.maximise_button = _TitleBarButton("expand")
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

    def _toggle_maximised(self) -> None:
        if self.isMaximized():
            self.showNormal()
        else:
            self.showMaximized()

    # -- frameless drag --------------------------------------------------
    #
    # A frameless window gets none of the window manager's move handling, so
    # the title bar has to do it. Tracked from the press position rather than
    # by deltas so a fast drag cannot accumulate rounding error.

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if (
            event.button() == Qt.MouseButton.LeftButton
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
