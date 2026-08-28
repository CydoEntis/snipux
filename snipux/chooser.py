"""The pre-snip chooser: the first thing a snip shows.

`docs/design/handoff-chooser.md` is the authority, and
`docs/design/Snipux Chooser.dc.html` is the behavioural one where that
document is ambiguous.

A single 54px row hanging from the top edge of the monitor the snip opened
on, asking the two questions that have to be answered before anything is
captured -- what to capture, and what should happen to it -- plus the delay,
which used to live in the floating bar's mode popover. That popover is gone;
this replaces it.

A stills/record switch sits alongside those (docs/design/recording.md
ticket 5): both the mode list and the "then" list change meaning when it
flips, which is why it is a fourth axis rather than another mode. UI and
state only -- nothing here is wired to a recorder.

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

    def __init__(self, value, icon_name, label, note="", shortcut="", parent=None, disabled=False):
        super().__init__(parent)
        metric = tokens.ChooserMetric
        self._value = value
        self._icon_name = icon_name
        self._label = label
        self._note = note
        self._shortcut = shortcut
        self._selected = False
        self._hovered = False
        self._disabled = disabled
        self.setMouseTracking(True)
        if not disabled:
            self.setCursor(Qt.CursorShape.PointingHandCursor)
        pad_v, _pad_h = metric.MENU_ROW_PAD
        self.setFixedHeight(pad_v * 2 + (34 if note else 18))

    def value(self):
        return self._value

    def set_selected(self, selected: bool) -> None:
        self._selected = selected
        self.update()

    def enterEvent(self, event) -> None:
        if not self._disabled:
            self._hovered = True
            self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._hovered = False
        self.update()
        super().leaveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        # Inert, not just greyed: a disabled row swallows the press (via
        # `_Surface`, so SNX-108 does not reopen) but never emits `clicked`.
        if self._disabled:
            return
        if self._released_inside(event):
            self.clicked.emit(self._value)

    def paintEvent(self, event) -> None:
        metric, colour = tokens.ChooserMetric, tokens.ChooserColor
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect())

        if self._disabled:
            fill = None
        elif self._selected:
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
        label_fg = QColor(
            colour.ROW_DISABLED_FG
            if self._disabled
            else (colour.ROW_SELECTED_FG if self._selected else colour.ROW_IDLE_FG)
        )
        if self._icon_name:
            size = metric.MENU_ROW_ICON
            pixmap = design.icon(self._icon_name, label_fg).pixmap(size, size)
            painter.drawPixmap(x, (self.height() - size) // 2, pixmap)
            x += size + 9

        # The tick's slot is reserved whether or not this row is the selected
        # one, so text does not reflow as the selection moves between rows.
        text_w = self.width() - x - pad_h - (metric.MENU_TICK + 8)

        painter.setFont(_font(12.5, 500))
        painter.setPen(label_fg)
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
        if self._selected and not self._disabled:
            size = metric.MENU_TICK
            pixmap = design.icon("check", tokens.Color.ACCENT).pixmap(size, size)
            painter.drawPixmap(right - size, (self.height() - size) // 2, pixmap)
            right -= size + 8
        if self._shortcut:
            painter.setFont(_font(10.5, 400, mono=True))
            # A disabled row's shortcut letter is dimmed the same as its
            # label/icon (`label_fg`, above) -- at full brightness it reads
            # as if the key still does something, when `handle_key` now
            # silently no-ops for it.
            painter.setPen(QColor(colour.ROW_DISABLED_FG if self._disabled else colour.SHORTCUT_FG))
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
        for value, icon_name, label, note, shortcut, disabled in rows:
            row = _MenuRow(value, icon_name, label, note, shortcut, self, disabled=disabled)
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
    """One of the row's three dropdown triggers: icon, label, chevron.

    An empty label makes it icon-only, which is what the capture-flow
    handoff asks of two of the three: *"Mode -- the only labelled
    control"*, destination *"icon only ... secondary decision, so no label
    on the trigger"*, delay *"label appears only when set"*. Labelling all
    three gave the row three equal-weight controls when only one of them
    is the question being asked, and made it wide enough to read as a
    toolbar rather than a sentence.
    """

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
        if not self._label:
            # Icon and chevron only, padded evenly: a labelled trigger's
            # left padding would look like a gap with nothing in it.
            self.setFixedWidth(
                round(
                    metric.TRIGGER_PAD_R + self._icon_size + 4
                    + metric.CHEVRON + metric.TRIGGER_PAD_R
                )
            )
            return
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

        size = self._icon_size
        x = metric.TRIGGER_PAD_R if not self._label else metric.TRIGGER_PAD_L
        pixmap = design.icon(self._icon_name, QColor(self._icon_colour)).pixmap(size, size)
        painter.drawPixmap(x, (self.height() - size) // 2, pixmap)

        if self._label:
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


class _KindSwitch(_Surface):
    """The stills/record axis: docs/design/recording.md ticket 5.

    The two sides are not symmetric -- the mode list and the "then" list
    both change meaning when this flips -- which is why it is its own
    control rather than another `_Trigger`/`_Menu` pair. It carries no
    chevron and opens no menu: there are only two states, so any click
    flips between them, the same as `Chooser.set_kind` documents.

A two-segment pill with a sliding highlight, not a boolean track+knob:
    an empty knob says on/off, not on/off *what*. Purely UI/state -- this
    ticket wires nothing to a recorder.

    The segments are glyphs, not words: the capture-flow handoff draws a
    camera and a filled dot, and "Record is a filled 10px circle, not a
    glyph" -- a red-adjacent dot is what every recorder in the world uses
    and needs no label. Spelling both out made the leading control the
    widest thing in the row, ahead of the mode it is only qualifying.
    """

    toggled = pyqtSignal()

    # (value, icon) -- `None` means the filled circle, which is drawn
    # rather than loaded: every icon set's "record" glyph is a circle
    # anyway, and a stroked one reads as a radio button.
    SEGMENTS = (("stills", "camera"), ("record", None))
    SEG_W = 30
    DOT = 10

    def __init__(self, parent=None):
        super().__init__(parent)
        metric = tokens.ChooserMetric
        self._kind = "stills"
        self._seg_widths = [0, 0]
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(metric.SWITCH_H)
        self._resize_to_fit()

    def set_kind(self, kind: str) -> None:
        self._kind = kind
        self.update()

    def _resize_to_fit(self) -> None:
        metric = tokens.ChooserMetric
        self._seg_widths = [self.SEG_W for _ in self.SEGMENTS]
        self.setFixedWidth(metric.SWITCH_PAD * 2 + sum(self._seg_widths))

    def mouseReleaseEvent(self, event) -> None:
        if self._released_inside(event):
            self.toggled.emit()

    def paintEvent(self, event) -> None:
        metric, colour = tokens.ChooserMetric, tokens.ChooserColor
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        radius = rect.height() / 2

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(design.chooser_color("SWITCH_TRACK"))
        painter.drawRoundedRect(rect, radius, radius)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(design.chooser_color("TRIGGER_BORDER"))
        painter.drawRoundedRect(rect, radius, radius)

        active = 0 if self._kind == "stills" else 1
        seg_x = [metric.SWITCH_PAD]
        for width in self._seg_widths[:-1]:
            seg_x.append(seg_x[-1] + width)

        highlight = QRectF(
            seg_x[active], metric.SWITCH_PAD,
            self._seg_widths[active], self.height() - metric.SWITCH_PAD * 2,
        )
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(design.chooser_color("SWITCH_HIGHLIGHT"))
        painter.drawRoundedRect(highlight, highlight.height() / 2, highlight.height() / 2)

        for index, (_value, icon_name) in enumerate(self.SEGMENTS):
            tint = QColor(colour.MODE_ACCENT if index == active else colour.ROW_IDLE_FG)
            centre = QRectF(
                seg_x[index], 0, self._seg_widths[index], self.height()
            ).center()
            if icon_name is None:
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(tint)
                painter.drawEllipse(centre, self.DOT / 2, self.DOT / 2)
                continue
            size = 16
            pixmap = design.icon(icon_name, tint).pixmap(size, size)
            painter.drawPixmap(
                round(centre.x() - size / 2), round(centre.y() - size / 2), pixmap
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

        self.kind_switch = _KindSwitch(self)
        row.addWidget(self.kind_switch)

        self.mode_trigger = _Trigger(
            "crop", "Region", 16, colour.MODE_ACCENT, self
        )
        self.mode_trigger.clicked.connect(lambda: self.triggerClicked.emit("mode"))
        row.addWidget(self.mode_trigger)

        # No "then" text node, and no label on this trigger. The
        # capture-flow handoff makes mode the only labelled control and
        # calls the destination a "secondary decision, so no label on the
        # trigger" -- and "then Review" reads as the sentence's main clause
        # when it is the part most users set once and never touch.
        self.after_trigger = _Trigger("eye", "", 15, colour.ROW_IDLE_FG, self)
        self.after_trigger.clicked.connect(lambda: self.triggerClicked.emit("after"))
        row.addWidget(self.after_trigger)

        self.delay_trigger = _Trigger("timer", "", 15, colour.ROW_IDLE_FG, self)
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
    kindChanged = pyqtSignal(str)

    def __init__(self, parent=None, *, screen_rect: QRectF | None = None, origin=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

        self._mode = tokens.CAPTURE_MODES[0][0]
        self._after = tokens.AFTER_DEFAULT
        # What the record side falls back to when this chooser switches to
        # it. A constant until recording's destination gained a Settings
        # row -- it could only ever be changed per-capture, on this
        # surface, with no way to say what it should open on. Seeded by
        # `OverlayWindow` from `setup_desktop.load_recording_after()`;
        # kept as a plain attribute with a token default so a Chooser built
        # without one (every test that does) behaves exactly as before.
        self._record_after_default = tokens.RECORD_AFTER_DEFAULT
        self._delay = tokens.DELAY_DEFAULT
        self._kind = "stills"
        self._phase = "choosing"
        self._menu: _Menu | None = None
        self._menu_kind: str | None = None

        self.panel = ChooserPanel(parent)
        self.panel.triggerClicked.connect(self._toggle_menu)
        self.panel.kind_switch.toggled.connect(self._toggle_kind)
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

    @property
    def kind(self) -> str:
        return self._kind

    def set_mode(self, mode: str, *, arm: bool = True) -> None:
        """Adopt `mode`, and by default arm it.

        `arm=False` is for the floating bar handing its own chip's value
        back: one piece of state across two surfaces, so a mode changed
        there and a chooser reopened afterwards agree -- without that change
        also collapsing a panel the user is looking at.
        """
        if mode not in dict((m[0], m) for m in tokens.CAPTURE_MODES):
            return
        if self._kind == "record" and mode in tokens.RECORD_DISABLED_MODES:
            # Window and Freeform aren't offered on the record side -- a
            # click on their disabled row is inert (`_MenuRow` already
            # swallows it), and a stray shortcut key must leave the current
            # mode alone the same way.
            return
        self._mode = mode
        self._refresh_triggers()
        if not arm:
            return
        if mode in tokens.IMMEDIATE_MODES and self._kind == "stills":
            # Nothing left to aim at, so choosing it *is* the capture -- but
            # only on the stills side. Nothing downstream knows how to
            # record yet, so on the record side Full screen arms and waits
            # like Region does, rather than silently taking a screenshot.
            self.fireImmediately.emit(mode)
            return
        self._phase = "armed"
        self.modeChosen.emit(mode)
        self._layout()

    def set_record_after_default(self, after: str) -> None:
        """Seed what the record side opens on, from Settings.

        Applied on the next switch *to* the record side rather than
        immediately: this is the default a record capture starts from, not
        an override of a choice already made on the surface -- the same
        distinction `set_after(arm=False)` draws for the stills side.
        """
        if after not in dict.fromkeys(value for value, *_ in _RECORD_AFTER_ROWS):
            return
        self._record_after_default = after
        if self._kind == "record":
            self._after = after
            self._refresh_triggers()

    def set_after(self, after: str) -> None:
        self._after = after
        self._refresh_triggers()

    def set_delay(self, delay: str) -> None:
        self._delay = delay
        self._refresh_triggers()

    def _toggle_kind(self) -> None:
        self.set_kind("record" if self._kind == "stills" else "stills")

    def set_kind(self, kind: str) -> None:
        """Flip stills/record. UI and state only -- nothing here starts a
        capture or a recording, per docs/design/recording.md ticket 5.

        Only `kind` changes, unless the current mode or destination has no
        meaning on the new side, in which case it snaps to one that does --
        assigned directly rather than through `set_mode`/`set_after`, so the
        snap itself never fires `modeChosen`/`fireImmediately`. Switching
        back to stills needs no such snap: its mode and after lists are the
        original, unrestricted ones.

        Emits `kindChanged` on every real flip -- unlike `after`/`delay`,
        there is no Settings surface for this axis, so the chooser itself is
        where "remembers which side was last used" has to be wired from;
        see `setup_desktop.save_kind`.
        """
        if kind not in ("stills", "record") or kind == self._kind:
            return
        self._kind = kind
        if kind == "record":
            if self._mode in tokens.RECORD_DISABLED_MODES:
                self._mode = tokens.CAPTURE_MODES[0][0]
            if self._after not in dict.fromkeys(value for value, *_ in _RECORD_AFTER_ROWS):
                self._after = self._record_after_default
        self._refresh_triggers()
        self.kindChanged.emit(kind)

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
            recording = self._kind == "record"
            rows = []
            for label, icon, note in tokens.CAPTURE_MODES:
                disabled = recording and label in tokens.RECORD_DISABLED_MODES
                if disabled:
                    # Why it cannot be picked outranks what it would do.
                    note = tokens.RECORD_DISABLED_MODES[label]
                elif recording:
                    note = tokens.RECORD_MODE_NOTE.get(label, note)
                rows.append((label, icon, label, note, keys.get(label, ""), disabled))
            return rows, self._mode, tokens.ChooserMetric.MENU_MODE_W
        if kind == "after":
            after_rows = _RECORD_AFTER_ROWS if self._kind == "record" else _AFTER_ROWS
            return [
                (identifier, icon, label, note, "", False)
                for identifier, icon, label, note in after_rows
            ], self._after, tokens.ChooserMetric.MENU_AFTER_W
        return [
            (value, "", value, "", "", False) for value in tokens.DELAYS
        ], self._delay, tokens.ChooserMetric.MENU_DELAY_W

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
            self.set_delay(value)

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
        self.panel.kind_switch.set_kind(self._kind)
        icon = dict((m[0], m[1]) for m in tokens.CAPTURE_MODES)[self._mode]
        self.panel.mode_trigger.set_content(icon, self._mode, colour.MODE_ACCENT)

        after_icon, after_label = _after_display(self._after, self._kind)
        # Icon only. The label still exists for the tab and the menu, which
        # have room for it and need it to be readable.
        self.panel.after_trigger.set_content(after_icon, "", colour.ROW_IDLE_FG)

        # A delay that is about to surprise you should be visible before it
        # does, so the label goes accent the moment one is set.
        armed_delay = self._delay != tokens.DELAY_DEFAULT
        # "Label appears only when set", per the handoff -- a countdown is
        # the one thing here that can surprise you, so it earns width
        # exactly when it is armed and none when it is not.
        self.panel.delay_trigger.set_content(
            "timer",
            self._delay if armed_delay else "",
            colour.MODE_ACCENT if armed_delay else colour.ROW_IDLE_FG,
            colour.MODE_ACCENT if armed_delay else tokens.Color.TEXT_PRIMARY,
        )

        next_step = tokens.MODE_NEXT_STEP.get(self._mode, "")
        if self._kind == "record":
            next_step = tokens.RECORD_MODE_NEXT_STEP.get(self._mode, next_step)
        self.hint.set_content(icon, next_step)
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
    ("instant", "copy", "Instant", tokens.CHOOSER_AFTER_NOTE["instant"]),
    ("edit", "pen", "Edit", tokens.CHOOSER_AFTER_NOTE["edit"]),
    ("review", "eye", "Review", tokens.CHOOSER_AFTER_NOTE["review"]),
]

# The record side's own "then" vocabulary -- Instant and Save, never Edit,
# Review or Trim. Kept out of `AFTER_CAPTURE`/`_AFTER_ROWS`, which are
# stills-only; "save" is not a destination stills can pick.
_RECORD_AFTER_ROWS = [
    ("instant", "copy", "Instant", tokens.CHOOSER_RECORD_AFTER_NOTE["instant"]),
    ("save", "save", "Save", tokens.CHOOSER_RECORD_AFTER_NOTE["save"]),
]


def _after_display(identifier: str, kind: str = "stills") -> tuple[str, str]:
    rows = _RECORD_AFTER_ROWS if kind == "record" else _AFTER_ROWS
    for value, icon, label, _note in rows:
        if value == identifier:
            return icon, label
    default = tokens.RECORD_AFTER_DEFAULT if kind == "record" else tokens.AFTER_DEFAULT
    return _after_display(default, kind)



