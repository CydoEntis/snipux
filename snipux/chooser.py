"""The pre-snip chooser: the first thing a snip shows.

`docs/design/handoff-chooser.md` is the authority, and
`docs/design/Snipux Chooser.dc.html` is the behavioural one where that
document is ambiguous.

A single 54px row hanging from the top edge of the monitor the snip opened
on, asking the two questions that have to be answered before anything is
captured -- what to capture, and what should happen to it -- plus the delay,
which used to live in the floating bar's mode popover. That popover is gone;
this replaces it.

It exists because capture mode used to live on the floating bar, and that
bar only appears once a selection does. Choosing "window" therefore meant
dragging out a region you did not want, clicking a chip, picking the mode
you actually wanted, and watching the region be thrown away.

Kept out of `overlay.py` deliberately: that file is already over 5,000 lines
and CLAUDE.md names splitting it as the obvious next cut. This is a whole
surface with its own state machine, so it starts in its own module rather
than making that worse.
"""

from __future__ import annotations

from typing import Callable

from PyQt6.QtCore import QPoint, QRect, QRectF, QSize, Qt, pyqtSignal
from PyQt6.QtGui import (
    QColor,
    QCursor,
    QFont,
    QFontMetricsF,
    QGuiApplication,
    QPainter,
    QPainterPath,
)
from PyQt6.QtWidgets import (
    QGraphicsDropShadowEffect,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from . import design
from .design import tokens


def _font(size: float, weight: int, mono: bool = False) -> QFont:
    families = design.font_families()
    font = QFont(families.mono if mono else families.ui)
    font.setPixelSize(round(size))
    font.setWeight(QFont.Weight(weight))
    return font


def _alpha(hex_colour: str, alpha: float) -> QColor:
    colour = QColor(hex_colour)
    colour.setAlphaF(alpha)
    return colour


def _rgba(hex_colour: str, alpha: float) -> str:
    colour = QColor(hex_colour)
    return f"rgba({colour.red()}, {colour.green()}, {colour.blue()}, {alpha:.2f})"


def _glass(painter, rect, radii, fill_alpha=tokens.ChooserColor.PANEL_BG_ALPHA) -> None:
    """The overlay's warm glass, with per-corner radii.

    `backdrop-filter: blur(16px)` has no Qt equivalent and the design says
    never to attempt a live one, so this is the documented fallback: a
    denser fill, no blur. The desktop behind is a static grab, so nothing
    moves under it to give the absence away.

    Alpha, not opacity: the panel is a 93%-alpha *fill* whose children are
    fully opaque. `setWindowOpacity(0.93)` would wash the icons out too.
    """
    top_left, top_right, bottom_right, bottom_left = radii
    path = QPainterPath()
    path.moveTo(rect.left() + top_left, rect.top())
    path.lineTo(rect.right() - top_right, rect.top())
    if top_right:
        path.quadTo(rect.right(), rect.top(), rect.right(), rect.top() + top_right)
    path.lineTo(rect.right(), rect.bottom() - bottom_right)
    if bottom_right:
        path.quadTo(rect.right(), rect.bottom(), rect.right() - bottom_right, rect.bottom())
    path.lineTo(rect.left() + bottom_left, rect.bottom())
    if bottom_left:
        path.quadTo(rect.left(), rect.bottom(), rect.left(), rect.bottom() - bottom_left)
    path.lineTo(rect.left(), rect.top() + top_left)
    if top_left:
        path.quadTo(rect.left(), rect.top(), rect.left() + top_left, rect.top())
    path.closeSubpath()

    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(_alpha(tokens.Color.BAR_BG, fill_alpha))
    painter.drawPath(path)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.setPen(design.chooser_color("TRIGGER_BORDER"))
    painter.drawPath(path)


class _Surface(QWidget):
    """A chooser widget that clicks, and so must swallow its own presses.

    Every widget on this surface is a child of the overlay, and a Qt widget
    that leaves a mouse press unaccepted lets it propagate to its parent.
    The overlay reads a press with no selection as the start of a region
    drag -- right for the overlay, whose docstring says so, and wrong for
    chrome sitting on top of it. Clicking `Region` therefore started a
    region capture instead of opening its menu (SNX-108), and the same was
    true of every other trigger, the tab, and the panel's own background.

    The press stops here; the click still happens on release. Subclasses
    that act on release must check `_released_inside` first: accepting the
    press makes this widget Qt's implicit mouse grabber, so a release that
    lands somewhere else entirely still arrives here, and pressing a
    control then sliding away from it is how a user says "no".

    The two passive pieces -- `_Pill` and `_Legend` -- deliberately stay out
    of this: nothing there is clickable, and a press that lands on them
    belongs to the drag underneath.

    `overlay._Chrome` is the same fix for the floating bar, the trays, the
    popovers, the toast and the HUD, which all had it too.
    """

    def mousePressEvent(self, event) -> None:
        event.accept()

    def _released_inside(self, event) -> bool:
        return (
            event.button() == Qt.MouseButton.LeftButton
            and self.rect().contains(event.position().toPoint())
        )


class _MenuRow(_Surface):
    """One row of a dropdown. Painted rather than styled because the
    destination menu's rows are two lines and the mode menu's carry a
    shortcut glyph and a tick -- more than a stylesheet can lay out.
    """

    clicked = pyqtSignal(str)

    def __init__(self, value, icon_name, label, note="", shortcut="", parent=None):
        super().__init__(parent)
        metric = tokens.ChooserMetric
        self._value = value
        self._icon_name = icon_name
        self._label = label
        self._note = note
        self._shortcut = shortcut
        self._selected = False
        self._hovered = False
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        pad_v, _pad_h = metric.MENU_ROW_PAD
        self.setFixedHeight(pad_v * 2 + (34 if note else 18))

    def value(self):
        return self._value

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

    def mouseReleaseEvent(self, event) -> None:
        if self._released_inside(event):
            self.clicked.emit(self._value)

    def paintEvent(self, event) -> None:
        metric, colour = tokens.ChooserMetric, tokens.ChooserColor
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect())

        if self._selected:
            fill = design.chooser_color("ROW_SELECTED_BG")
        elif self._hovered:
            fill = design.chooser_color("ROW_HOVER_BG")
        else:
            fill = None
        if fill is not None:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(fill)
            painter.drawRoundedRect(rect, metric.MENU_ROW_RADIUS, metric.MENU_ROW_RADIUS)

        pad_v, pad_h = metric.MENU_ROW_PAD
        x = pad_h
        if self._icon_name:
            size = metric.MENU_ROW_ICON
            glyph = QColor(colour.ROW_SELECTED_FG if self._selected else colour.ROW_IDLE_FG)
            pixmap = design.icon(self._icon_name, glyph).pixmap(size, size)
            painter.drawPixmap(x, (self.height() - size) // 2, pixmap)
            x += size + 9

        # The tick's slot is reserved whether or not this row is the selected
        # one, so text does not reflow as the selection moves between rows.
        text_w = self.width() - x - pad_h - (metric.MENU_TICK + 8)

        painter.setFont(_font(12.5, 500))
        painter.setPen(QColor(colour.ROW_SELECTED_FG if self._selected else colour.ROW_IDLE_FG))
        if self._note:
            label = QFontMetricsF(painter.font()).elidedText(
                self._label, Qt.TextElideMode.ElideRight, text_w
            )
            painter.drawText(x, pad_v + 13, label)
            painter.setFont(_font(11, 400))
            painter.setPen(QColor(colour.HINT_FG))
            # Elide rather than trust the string: the menu is a fixed width
            # and a note is prose, so a font substitution on someone else's
            # machine must degrade to an ellipsis, not run off the panel.
            note = QFontMetricsF(painter.font()).elidedText(
                self._note, Qt.TextElideMode.ElideRight, text_w
            )
            painter.drawText(x, pad_v + 30, note)
        else:
            painter.drawText(
                QRectF(x, 0, self.width() - x, self.height()),
                Qt.AlignmentFlag.AlignVCenter,
                self._label,
            )

        right = self.width() - pad_h
        if self._selected:
            size = metric.MENU_TICK
            pixmap = design.icon("check", tokens.Color.ACCENT).pixmap(size, size)
            painter.drawPixmap(right - size, (self.height() - size) // 2, pixmap)
            right -= size + 8
        if self._shortcut:
            painter.setFont(_font(10.5, 400, mono=True))
            painter.setPen(QColor(colour.SHORTCUT_FG))
            painter.drawText(
                QRectF(right - 20, 0, 20, self.height()),
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight,
                self._shortcut,
            )
        painter.end()


class _Menu(QWidget):
    """A dropdown. A frameless `Qt.Popup`, not a `QMenu`.

    `QMenu` will not take this styling cleanly, and the destination menu's
    two-line rows need a size hint of their own -- so the rows are real
    child widgets and this is the frame around them. Being a popup is also
    what lets it paint outside the panel's bounds and close on a click
    anywhere else.
    """

    picked = pyqtSignal(str)
    closed = pyqtSignal()

    def __init__(self, width: int, rows, selected, parent=None):
        super().__init__(parent, Qt.WindowType.Popup)
        metric = tokens.ChooserMetric
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

        column = QVBoxLayout(self)
        column.setContentsMargins(
            metric.MENU_PAD, metric.MENU_PAD, metric.MENU_PAD, metric.MENU_PAD
        )
        column.setSpacing(1)
        self._rows: dict[str, _MenuRow] = {}
        for value, icon_name, label, note, shortcut in rows:
            row = _MenuRow(value, icon_name, label, note, shortcut, self)
            row.set_selected(value == selected)
            row.clicked.connect(self.picked)
            self._rows[value] = row
            column.addWidget(row)
        self.setFixedWidth(width)
        self.adjustSize()

    def closeEvent(self, event) -> None:
        self.closed.emit()
        super().closeEvent(event)

    def paintEvent(self, event) -> None:
        metric, colour = tokens.ChooserMetric, tokens.ChooserColor
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(design.chooser_color("MENU_BG"))
        painter.drawRoundedRect(rect, metric.MENU_RADIUS, metric.MENU_RADIUS)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(design.chooser_color("MENU_BORDER"))
        painter.drawRoundedRect(rect, metric.MENU_RADIUS, metric.MENU_RADIUS)
        painter.end()


class _Trigger(_Surface):
    """One of the row's three dropdown triggers: icon, label, chevron."""

    clicked = pyqtSignal()

    def __init__(self, icon_name, label, icon_size, icon_colour, parent=None):
        super().__init__(parent)
        metric = tokens.ChooserMetric
        self._icon_name = icon_name
        self._label = label
        self._icon_size = icon_size
        self._icon_colour = icon_colour
        self._open = False
        self._hovered = False
        self._label_colour = tokens.Color.TEXT_PRIMARY
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(metric.TRIGGER_H)
        self._resize_to_fit()

    def set_open(self, is_open: bool) -> None:
        self._open = is_open
        self.update()

    def set_content(self, icon_name, label, icon_colour=None, label_colour=None) -> None:
        self._icon_name = icon_name
        self._label = label
        if icon_colour is not None:
            self._icon_colour = icon_colour
        self._label_colour = label_colour or tokens.Color.TEXT_PRIMARY
        self._resize_to_fit()
        self.update()

    def _resize_to_fit(self) -> None:
        from PyQt6.QtGui import QFontMetricsF

        metric = tokens.ChooserMetric
        text = QFontMetricsF(_font(12.5, 500)).horizontalAdvance(self._label)
        self.setFixedWidth(
            round(
                metric.TRIGGER_PAD_L + self._icon_size + 8 + text + 6
                + metric.CHEVRON + metric.TRIGGER_PAD_R
            )
        )

    def enterEvent(self, event) -> None:
        self._hovered = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._hovered = False
        self.update()
        super().leaveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if self._released_inside(event):
            self.clicked.emit()

    def paintEvent(self, event) -> None:
        metric, colour = tokens.ChooserMetric, tokens.ChooserColor
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)

        # The open trigger is visibly the source of the menu.
        if self._open:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(design.chooser_color("TRIGGER_BG_OPEN"))
            painter.drawRoundedRect(rect, metric.TRIGGER_RADIUS, metric.TRIGGER_RADIUS)
        elif self._hovered:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(_alpha(colour.TRIGGER_BG_OPEN, 0.05))
            painter.drawRoundedRect(rect, metric.TRIGGER_RADIUS, metric.TRIGGER_RADIUS)

        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(
            design.chooser_color(
                "TRIGGER_BORDER_OPEN" if self._open else "TRIGGER_BORDER"
            )
        )
        painter.drawRoundedRect(rect, metric.TRIGGER_RADIUS, metric.TRIGGER_RADIUS)

        x = metric.TRIGGER_PAD_L
        size = self._icon_size
        pixmap = design.icon(self._icon_name, QColor(self._icon_colour)).pixmap(size, size)
        painter.drawPixmap(x, (self.height() - size) // 2, pixmap)
        x += size + 8

        painter.setFont(_font(12.5, 500))
        painter.setPen(QColor(self._label_colour))
        painter.drawText(
            QRectF(x, 0, self.width() - x, self.height()),
            Qt.AlignmentFlag.AlignVCenter,
            self._label,
        )

        chevron = metric.CHEVRON
        pixmap = design.icon("chevron", QColor(colour.HINT_FG)).pixmap(chevron, chevron)
        painter.drawPixmap(
            self.width() - metric.TRIGGER_PAD_R - chevron,
            (self.height() - chevron) // 2,
            pixmap,
        )
        painter.end()


class _Pill(QWidget):
    """The hint line: an icon and a sentence, on its own small glass pill."""

    def __init__(self, parent=None):
        super().__init__(parent)
        metric = tokens.ChooserMetric
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setFixedHeight(metric.HINT_H)
        self._icon_name = "crop"
        self._text = ""

    def set_content(self, icon_name: str, text: str) -> None:
        from PyQt6.QtGui import QFontMetricsF

        metric = tokens.ChooserMetric
        self._icon_name, self._text = icon_name, text
        pad_v, pad_h = metric.HINT_PAD
        width = QFontMetricsF(_font(11.5, 400)).horizontalAdvance(text)
        self.setFixedWidth(round(pad_h * 2 + 14 + 7 + width))
        self.update()

    def paintEvent(self, event) -> None:
        metric, colour = tokens.ChooserMetric, tokens.ChooserColor
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(design.chooser_color("HINT_BG"))
        painter.drawRoundedRect(rect, metric.HINT_RADIUS, metric.HINT_RADIUS)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(design.chooser_color("HINT_BORDER"))
        painter.drawRoundedRect(rect, metric.HINT_RADIUS, metric.HINT_RADIUS)

        _pad_v, pad_h = metric.HINT_PAD
        pixmap = design.icon(self._icon_name, QColor(colour.MODE_ACCENT)).pixmap(14, 14)
        painter.drawPixmap(pad_h, (self.height() - 14) // 2, pixmap)
        painter.setFont(_font(11.5, 400))
        painter.setPen(QColor(colour.HINT_FG))
        painter.drawText(
            QRectF(pad_h + 14 + 7, 0, self.width(), self.height()),
            Qt.AlignmentFlag.AlignVCenter,
            self._text,
        )
        painter.end()


class _Legend(QWidget):
    """`R W F L` mode · `Space` reopen · `Esc` cancel, bottom centre."""

    PARTS = (
        ("R W F L", " mode"),
        ("Space", " reopen"),
        ("Esc", " cancel"),
    )

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setFixedHeight(tokens.ChooserMetric.LEGEND_H)
        self._measure()

    def _measure(self) -> None:
        from PyQt6.QtGui import QFontMetricsF

        keys = QFontMetricsF(_font(11.5, 500, mono=True))
        body = QFontMetricsF(_font(11.5, 400))
        width = 26
        for key, label in self.PARTS:
            width += keys.horizontalAdvance(key) + body.horizontalAdvance(label) + 18
        self.setFixedWidth(round(width))

    def paintEvent(self, event) -> None:
        colour = tokens.ChooserColor
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(_alpha(tokens.Color.BAR_BG, tokens.ChooserColor.PANEL_BG_ALPHA))
        painter.drawRoundedRect(rect, 10, 10)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(design.chooser_color("TRIGGER_BORDER"))
        painter.drawRoundedRect(rect, 10, 10)

        from PyQt6.QtGui import QFontMetricsF

        keys_font, body_font = _font(11.5, 500, mono=True), _font(11.5, 400)
        keys, body = QFontMetricsF(keys_font), QFontMetricsF(body_font)
        x = 13.0
        for index, (key, label) in enumerate(self.PARTS):
            painter.setFont(keys_font)
            painter.setPen(QColor(colour.LEGEND_KEY_FG))
            painter.drawText(
                QRectF(x, 0, keys.horizontalAdvance(key), self.height()),
                Qt.AlignmentFlag.AlignVCenter,
                key,
            )
            x += keys.horizontalAdvance(key)
            painter.setFont(body_font)
            painter.setPen(QColor(colour.HINT_FG))
            tail = label if index == len(self.PARTS) - 1 else f"{label}  ·"
            painter.drawText(
                QRectF(x, 0, body.horizontalAdvance(tail) + 8, self.height()),
                Qt.AlignmentFlag.AlignVCenter,
                tail,
            )
            x += body.horizontalAdvance(tail) + 10
        painter.end()


class _Tab(_Surface):
    """What the panel collapses to once a mode is armed.

    26px of the monitor's top edge -- which on GNOME is the top bar's
    territory anyway, so in practice it costs nothing that was not already
    spoken for. Window previews and Freeform tracing work everywhere below.

    This is the one place `windowOpacity`-style translucency is genuinely
    right: the whole widget really is see-through at rest, so an opacity
    effect is correct here where it would be wrong on the panel.
    """

    clicked = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        metric = tokens.ChooserMetric
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(metric.TAB_H)
        self._mode = ""
        self._icon_name = "crop"
        self._tail = ""
        self._delay = ""
        self._effect = QGraphicsOpacityEffect(self)
        self._effect.setOpacity(metric.TAB_OPACITY)
        self.setGraphicsEffect(self._effect)

    def set_content(self, icon_name, mode, tail, delay) -> None:
        from PyQt6.QtGui import QFontMetricsF

        metric = tokens.ChooserMetric
        self._icon_name, self._mode, self._tail, self._delay = icon_name, mode, tail, delay
        width = metric.TAB_PAD_H * 2 + 15 + 7
        width += QFontMetricsF(_font(12, 500)).horizontalAdvance(mode) + 6
        width += QFontMetricsF(_font(11.5, 400)).horizontalAdvance(tail) + 6
        if delay:
            width += QFontMetricsF(_font(11, 400, mono=True)).horizontalAdvance(delay) + 8
        width += metric.CHEVRON + 4
        self.setFixedWidth(round(width))
        self.update()

    def enterEvent(self, event) -> None:
        self._effect.setOpacity(1.0)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._effect.setOpacity(tokens.ChooserMetric.TAB_OPACITY)
        super().leaveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if self._released_inside(event):
            self.clicked.emit()

    def paintEvent(self, event) -> None:
        from PyQt6.QtGui import QFontMetricsF

        metric, colour = tokens.ChooserMetric, tokens.ChooserColor
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        _glass(painter, QRectF(self.rect()), metric.TAB_RADIUS, metric.TAB_BG_ALPHA)

        x = float(metric.TAB_PAD_H)
        pixmap = design.icon(self._icon_name, QColor(colour.MODE_ACCENT)).pixmap(15, 15)
        painter.drawPixmap(round(x), (self.height() - 15) // 2, pixmap)
        x += 15 + 7

        painter.setFont(_font(12, 500))
        painter.setPen(QColor(colour.MODE_ACCENT))
        painter.drawText(
            QRectF(x, 0, self.width(), self.height()),
            Qt.AlignmentFlag.AlignVCenter,
            self._mode,
        )
        x += QFontMetricsF(_font(12, 500)).horizontalAdvance(self._mode) + 6

        painter.setFont(_font(11.5, 400))
        painter.setPen(QColor(colour.HINT_FG))
        painter.drawText(
            QRectF(x, 0, self.width(), self.height()),
            Qt.AlignmentFlag.AlignVCenter,
            self._tail,
        )
        x += QFontMetricsF(_font(11.5, 400)).horizontalAdvance(self._tail) + 6

        if self._delay:
            painter.setFont(_font(11, 400, mono=True))
            painter.setPen(QColor(colour.MODE_ACCENT))
            painter.drawText(
                QRectF(x, 0, self.width(), self.height()),
                Qt.AlignmentFlag.AlignVCenter,
                self._delay,
            )

        chevron = metric.CHEVRON
        pixmap = design.icon("chevron", QColor(colour.HINT_FG)).pixmap(chevron, chevron)
        painter.drawPixmap(
            self.width() - metric.TAB_PAD_H - chevron,
            (self.height() - chevron) // 2,
            pixmap,
        )
        painter.end()


class ChooserPanel(_Surface):
    """The 54px row itself: mode, "then", destination, delay.

    Square top corners and 14px bottom corners, with no top border, so it
    reads as hanging from the monitor's edge rather than floating near it.
    That is the visual claim that this bar belongs to this monitor.
    """

    triggerClicked = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        metric = tokens.ChooserMetric
        colour = tokens.ChooserColor
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setFixedHeight(metric.HEIGHT)

        row = QHBoxLayout(self)
        row.setContentsMargins(metric.PAD, metric.PAD, metric.PAD, metric.PAD)
        row.setSpacing(metric.GAP)

        self.mode_trigger = _Trigger(
            "crop", "Region", 16, colour.MODE_ACCENT, self
        )
        self.mode_trigger.clicked.connect(lambda: self.triggerClicked.emit("mode"))
        row.addWidget(self.mode_trigger)

        # A literal text node, not decoration: it is what makes the row parse
        # as one sentence rather than three unrelated widgets.
        then = QLabel("then", self)
        then.setFont(_font(12, 400))
        then.setStyleSheet(f"color: {colour.HINT_FG}; background: transparent;")
        then.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        row.addWidget(then)

        self.after_trigger = _Trigger("eye", "Review", 15, colour.ROW_IDLE_FG, self)
        self.after_trigger.clicked.connect(lambda: self.triggerClicked.emit("after"))
        row.addWidget(self.after_trigger)

        self.delay_trigger = _Trigger(
            "timer", tokens.DELAY_DEFAULT, 15, colour.ROW_IDLE_FG, self
        )
        self.delay_trigger.clicked.connect(lambda: self.triggerClicked.emit("delay"))
        row.addWidget(self.delay_trigger)

        self.adjustSize()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        _glass(painter, QRectF(self.rect()), tokens.ChooserMetric.RADIUS)
        painter.end()


class Chooser(QWidget):
    """The whole surface and its state machine.

    Two phases, and the second is the design problem the handoff is mostly
    about. **Choosing**: the full row, the hint, the legend. **Armed**: the
    row collapses to a 26px tab still hanging from the same edge, because
    Region, Window and Freeform all need the screen back -- one to drag on,
    one to hover over, one to trace across.

    Full screen is the exception and does not arm at all: it has nothing
    left to aim at, so choosing it fires the grab. `IMMEDIATE_MODES` carries
    that rather than a branch on the mode's name.

    Everything positions against `screen`, the monitor the snip opened on --
    never the virtual desktop, which on a staggered multi-monitor setup puts
    the panel in a gap between screens.
    """

    modeChosen = pyqtSignal(str)
    fireImmediately = pyqtSignal(str)
    cancelled = pyqtSignal()

    def __init__(self, parent=None, *, screen_rect: QRectF | None = None, origin=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

        self._mode = tokens.CAPTURE_MODES[0][0]
        self._after = tokens.AFTER_CAPTURE[0][0]
        self._delay = tokens.DELAY_DEFAULT
        self._phase = "choosing"
        self._menu: _Menu | None = None
        self._menu_kind: str | None = None

        self.panel = ChooserPanel(parent)
        self.panel.triggerClicked.connect(self._toggle_menu)
        self.hint = _Pill(parent)
        self.tab = _Tab(parent)
        self.tab.clicked.connect(self.reopen)
        self.legend = _Legend(parent)

        shadow = QGraphicsDropShadowEffect(self.panel)
        shadow.setBlurRadius(48)
        shadow.setOffset(0, 22)
        shadow.setColor(_alpha("#000000", 0.62))
        self.panel.setGraphicsEffect(shadow)

        self._screen_rect = screen_rect
        self._origin = origin or QPoint(0, 0)
        self._refresh_triggers()

    # -- state -----------------------------------------------------------

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def after(self) -> str:
        return self._after

    @property
    def delay(self) -> str:
        return self._delay

    @property
    def phase(self) -> str:
        return self._phase

    def set_mode(self, mode: str, *, arm: bool = True) -> None:
        """Adopt `mode`, and by default arm it.

        `arm=False` is for the floating bar handing its own chip's value
        back: one piece of state across two surfaces, so a mode changed
        there and a chooser reopened afterwards agree -- without that change
        also collapsing a panel the user is looking at.
        """
        if mode not in dict((m[0], m) for m in tokens.CAPTURE_MODES):
            return
        self._mode = mode
        self._refresh_triggers()
        if not arm:
            return
        if mode in tokens.IMMEDIATE_MODES:
            # Nothing left to aim at, so choosing it *is* the capture.
            self.fireImmediately.emit(mode)
            return
        self._phase = "armed"
        self.modeChosen.emit(mode)
        self._layout()

    def set_after(self, after: str) -> None:
        self._after = after
        self._refresh_triggers()

    def set_delay(self, delay: str) -> None:
        self._delay = delay
        self._refresh_triggers()

    def reopen(self) -> None:
        """Back to choosing, with every selection intact."""
        self._phase = "choosing"
        self._layout()

    # -- keyboard --------------------------------------------------------

    def handle_key(self, key: int, text: str) -> bool:
        """Returns True if the key was the chooser's.

        Live in both phases, per the handoff -- the shortcuts are how you
        change mode without reopening anything.
        """
        if key == Qt.Key.Key_Escape:
            if self._close_menu():
                return True
            self.cancelled.emit()
            return True
        if key == Qt.Key.Key_Space and self._phase == "armed":
            self.reopen()
            return True
        mode = tokens.MODE_KEYS.get((text or "").upper())
        if mode is not None:
            self._close_menu()
            self.set_mode(mode)
            return True
        return False

    # -- menus -----------------------------------------------------------

    def _rows_for(self, kind: str):
        if kind == "mode":
            keys = {mode: key for key, mode in tokens.MODE_KEYS.items()}
            return [
                (label, icon, label, "", keys.get(label, ""))
                for label, icon, _hint in tokens.CAPTURE_MODES
            ], self._mode, tokens.ChooserMetric.MENU_MODE_W
        if kind == "after":
            return [
                (identifier, icon, label, note, "")
                for identifier, icon, label, note in _AFTER_ROWS
            ], self._after, tokens.ChooserMetric.MENU_AFTER_W
        return [
            (value, "", _delay_label(value), "", "") for value in tokens.DELAYS
        ], _delay_value(self._delay), tokens.ChooserMetric.MENU_DELAY_W

    def _toggle_menu(self, kind: str) -> None:
        # One at a time: opening one closes the others.
        if self._menu_kind == kind:
            self._close_menu()
            return
        self._close_menu()
        rows, selected, width = self._rows_for(kind)
        menu = _Menu(width, rows, selected, self.panel)
        menu.picked.connect(lambda value, k=kind: self._on_picked(k, value))
        self._menu, self._menu_kind = menu, kind
        trigger = self._trigger_for(kind)
        trigger.set_open(True)
        origin = trigger.mapToGlobal(QPoint(0, tokens.ChooserMetric.MENU_OFFSET_Y - 34 + 34))
        menu.move(origin.x(), origin.y() + 7)
        menu.show()

    def _on_picked(self, kind: str, value: str) -> None:
        self._close_menu()
        if kind == "mode":
            self.set_mode(value)
        elif kind == "after":
            self.set_after(value)
        else:
            self.set_delay(_delay_label(value))

    def _close_menu(self) -> bool:
        if self._menu is None:
            return False
        self._trigger_for(self._menu_kind).set_open(False)
        self._menu.close()
        self._menu = self._menu_kind = None
        return True

    def _trigger_for(self, kind: str) -> _Trigger:
        return {
            "mode": self.panel.mode_trigger,
            "after": self.panel.after_trigger,
            "delay": self.panel.delay_trigger,
        }[kind]

    # -- painting the row's contents -------------------------------------

    def _refresh_triggers(self) -> None:
        colour = tokens.ChooserColor
        icon = dict((m[0], m[1]) for m in tokens.CAPTURE_MODES)[self._mode]
        self.panel.mode_trigger.set_content(icon, self._mode, colour.MODE_ACCENT)

        after_icon, after_label = _after_display(self._after)
        self.panel.after_trigger.set_content(after_icon, after_label, colour.ROW_IDLE_FG)

        # A delay that is about to surprise you should be visible before it
        # does, so the label goes accent the moment one is set.
        armed_delay = self._delay != tokens.DELAY_DEFAULT
        self.panel.delay_trigger.set_content(
            "timer",
            self._delay,
            colour.MODE_ACCENT if armed_delay else colour.ROW_IDLE_FG,
            colour.MODE_ACCENT if armed_delay else tokens.Color.TEXT_PRIMARY,
        )

        self.hint.set_content(icon, tokens.MODE_NEXT_STEP.get(self._mode, ""))
        self.tab.set_content(
            icon, self._mode, f"then {after_label}",
            "" if not armed_delay else self._delay,
        )
        self.panel.adjustSize()
        self._layout()

    # -- geometry --------------------------------------------------------

    def set_screen(self, screen_rect: QRectF, origin: QPoint) -> None:
        """`screen_rect` is the active monitor in absolute coordinates;
        `origin` is the host window's own top-left, so everything can be
        placed in window-local space.
        """
        self._screen_rect, self._origin = screen_rect, origin
        self._layout()

    def _layout(self) -> None:
        if self._screen_rect is None:
            return
        metric = tokens.ChooserMetric
        rect = self._screen_rect
        left = rect.x() - self._origin.x()
        top = rect.y() - self._origin.y()
        centre = left + rect.width() / 2

        choosing = self._phase == "choosing"

        panel_size = self.panel.sizeHint()
        self.panel.setGeometry(
            round(centre - panel_size.width() / 2), round(top),
            panel_size.width(), metric.HEIGHT,
        )
        self.panel.setVisible(choosing)

        self.hint.move(
            round(centre - self.hint.width() / 2),
            round(top + (metric.HEIGHT + metric.HINT_GAP if choosing else metric.ARMED_HINT_TOP)),
        )
        self.hint.show()
        self.hint.raise_()

        self.tab.move(round(centre - self.tab.width() / 2), round(top))
        self.tab.setVisible(not choosing)
        self.tab.raise_()

        self.legend.move(
            round(centre - self.legend.width() / 2),
            round(top + rect.height() - metric.LEGEND_BOTTOM - metric.LEGEND_H),
        )
        self.legend.show()
        self.legend.raise_()
        if choosing:
            self.panel.raise_()

    def hide_all(self) -> None:
        self._close_menu()
        for widget in (self.panel, self.hint, self.tab, self.legend):
            widget.hide()


# `AFTER_CAPTURE` carries the ids and the Settings pane's long prose; this
# binds each id to the glyph and short label the chooser draws, with the note
# coming from `CHOOSER_AFTER_NOTE`. The ids stay the shared spine, so a
# destination cannot exist on one surface and not the other.
_AFTER_ROWS = [
    ("review", "eye", "Review", tokens.CHOOSER_AFTER_NOTE["review"]),
    ("clip", "copy", "Copy", tokens.CHOOSER_AFTER_NOTE["clip"]),
    ("file", "save", "Save", tokens.CHOOSER_AFTER_NOTE["file"]),
]


def _after_display(identifier: str) -> tuple[str, str]:
    for value, icon, label, _note in _AFTER_ROWS:
        if value == identifier:
            return icon, label
    return "eye", "Review"


def _delay_label(value: str) -> str:
    """`tokens.DELAYS` stores "Off"; the chooser prints it in full."""
    return tokens.DELAY_DEFAULT if value == "Off" else value


def _delay_value(label: str) -> str:
    return "Off" if label == tokens.DELAY_DEFAULT else label
