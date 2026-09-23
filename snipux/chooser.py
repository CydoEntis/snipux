"""The pre-snip chooser: the row a snip opens with.

`docs/design/bars/README.md` section 1 is the design; the spec,
`docs/design/bars/reference/Snipux Handoff Preview.dc.html`, settles what it
leaves ambiguous; and `docs/design/bars/divergences.md` overrides both
wherever this build differs.

One 42px row hanging from the top edge of the monitor being worked on:

    [camera  dot]  [glyph Region v]  |  [destination]  [eyeOff  timer]
         kind            mode                               flags

Mode is the only label. Every other control is an icon that shows its state,
and the explanation lives in its tooltip and in the hint pill under the row.
Last region is a mode, at the foot of the mode menu.

Picking a mode does not arm it. The row stays up until the user drags or
clicks a window; the modes with nothing left to aim at capture on the pick.
Once a selection exists the row collapses to a 22px tab on the same edge,
and the tab or Space opens it again with everything as it was.

UI and state only. `OverlayWindow` decides what a mode does, which monitor
the row hangs from, and when a selection exists. Kept out of `overlay.py`
because it is a whole surface with its own state machine, and that file is
already several thousand lines.
"""

from __future__ import annotations

import math
from typing import NamedTuple

from PyQt6.QtCore import QPoint, QRectF, QSizeF, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QFontMetricsF, QPainter, QPainterPath
from PyQt6.QtWidgets import (
    QGraphicsDropShadowEffect,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLayout,
    QVBoxLayout,
    QWidget,
)

from . import design, glass
from .flowbars import MenuReopenGuard
from .design import tokens


def _font(spec, mono: bool = False) -> QFont:
    """A QFont for a `tokens.BarFont` (px, weight) pair, in the face that
    resolves on this machine -- IBM Plex where installed, a platform face
    otherwise (docs/design/bars/divergences.md, "Fonts")."""
    size, weight = spec
    families = design.font_families()
    font = QFont(families.mono if mono else families.ui)
    font.setPixelSize(round(size))
    font.setWeight(QFont.Weight(weight))
    return font


def _advance(text: str, spec, mono: bool = False) -> int:
    """Logical width of `text`, rounded up so a label is never clipped.

    Measured, never trusted to a token: every width here that holds text is
    tuned to Plex, and a fallback face has different advances.
    """
    return math.ceil(QFontMetricsF(_font(spec, mono)).horizontalAdvance(text))


def _colour(name: str) -> QColor:
    return design.bar_color(name)


def _docked_path(rect: QRectF, radii, *, closed: bool) -> QPainterPath:
    """The outline of a surface hanging from the top edge of a monitor.

    `radii` is (top-left, top-right, bottom-right, bottom-left). Closed, the
    path is the fill. Open, it is the border, which has no top edge: the row
    hangs from the monitor's edge rather than floating near it, and a line
    along that edge would draw exactly the gap the design closes.
    """
    top_left, top_right, bottom_right, bottom_left = radii
    left, top, right, bottom = rect.left(), rect.top(), rect.right(), rect.bottom()
    path = QPainterPath()
    if closed:
        path.moveTo(left + top_left, top)
        path.lineTo(right - top_right, top)
        if top_right:
            path.arcTo(QRectF(right - 2 * top_right, top, 2 * top_right, 2 * top_right), 90, -90)
    else:
        path.moveTo(right, top + top_right)
    path.lineTo(right, bottom - bottom_right)
    if bottom_right:
        path.arcTo(
            QRectF(right - 2 * bottom_right, bottom - 2 * bottom_right,
                   2 * bottom_right, 2 * bottom_right),
            0, -90,
        )
    path.lineTo(left + bottom_left, bottom)
    if bottom_left:
        path.arcTo(
            QRectF(left, bottom - 2 * bottom_left, 2 * bottom_left, 2 * bottom_left), 270, -90
        )
    path.lineTo(left, top + top_left)
    if closed:
        if top_left:
            path.arcTo(QRectF(left, top, 2 * top_left, 2 * top_left), 180, -90)
        path.closeSubpath()
    return path


def _paint_docked(painter: QPainter, widget: "_Surface", radii, fill: QColor, border: QColor) -> None:
    """Fill and border a docked surface, on its glass: the frame behind it
    blurred, under the fill (`snipux.glass`).

    Alpha, never opacity: the fill is translucent and everything painted on
    it afterwards is fully opaque. `windowOpacity` would wash the icons out
    with the ground.
    """
    rect = QRectF(widget.rect())
    widget.glass.paint(painter, _docked_path(rect, radii, closed=True), fill)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.setPen(border)
    # Half a pixel in, so the 1px border lands on whole logical pixels.
    painter.drawPath(_docked_path(rect.adjusted(0.5, 0, -0.5, -0.5), radii, closed=False))


def _draw_icon(painter: QPainter, name: str, colour: QColor, x: float, y: float, size: int) -> None:
    pixmap = design.icon(name, colour).pixmap(size, size)
    painter.drawPixmap(round(x), round(y), pixmap)


class _Surface(QWidget):
    """A chooser widget that clicks, and so must swallow its own presses.

    Every widget on this surface is a child of the overlay, and a Qt widget
    that leaves a mouse press unaccepted lets it propagate to its parent.
    The overlay reads a press with no selection as the start of a region
    drag -- right for the overlay, wrong for chrome sitting on top of it.
    Clicking the mode control once started a region capture instead of
    opening its menu (SNX-108). The handoff calls this the one that will
    bite: "Chrome must stop pointer propagation."

    The press stops here; the click happens on release. A release that
    lands somewhere else still arrives here, because accepting the press
    makes this widget Qt's implicit mouse grabber -- and pressing a control
    then sliding away from it is how a user says "no", so subclasses check
    `_released_inside` first.

    `hovered` is how the row borrows the hint pill to explain a control:
    Qt's own tooltips are a coin toss on an always-on-top frameless window.
    It carries the control, so one bound method can listen to them all -- a
    lambda closing over the listener would keep it alive as long as the
    control, which for a chooser without a parent is for ever.

    `glass` is what a surface paints its ground on: the frame behind it,
    blurred, under the fill (`snipux.glass`). Here, so every surface on the
    row has one to paint with.
    """

    hovered = pyqtSignal(QWidget, bool)

    def __init__(self, parent=None, *, clickable: bool = True):
        super().__init__(parent)
        self.glass = glass.Glass(self)
        self._hovered = False
        self.setMouseTracking(True)
        self.setCursor(
            Qt.CursorShape.PointingHandCursor if clickable else Qt.CursorShape.ArrowCursor
        )

    def enterEvent(self, event) -> None:
        self._hovered = True
        self.update()
        self.hovered.emit(self, True)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._hovered = False
        self.update()
        self.hovered.emit(self, False)
        super().leaveEvent(event)

    def mousePressEvent(self, event) -> None:
        event.accept()

    def _released_inside(self, event) -> bool:
        return (
            event.button() == Qt.MouseButton.LeftButton
            and self.rect().contains(event.position().toPoint())
        )


class _KindButton(_Surface):
    """One side of the stills/record pair.

    The two are a pair of buttons in a well rather than a switch, because a
    switch's knob says on/off and not on/off *what*. Both are glyphs of the
    thing they make -- a camera and a camcorder -- rather than the handoff's
    filled circle for record: a dot beside a camera says "on", not "a video"
    (bars/divergences.md 25).
    """

    picked = pyqtSignal(str)

    def __init__(self, kind: str, parent=None):
        super().__init__(parent)
        self.kind = kind
        self._active = False
        self.setFixedSize(tokens.BarMetric.BTN, tokens.BarMetric.BTN)
        self.setToolTip(tokens.KIND_TOOLTIP[kind])

    def is_active(self) -> bool:
        return self._active

    def set_active(self, active: bool) -> None:
        self._active = bool(active)
        self.update()

    def mouseReleaseEvent(self, event) -> None:
        if self._released_inside(event):
            self.picked.emit(self.kind)

    def paintEvent(self, event) -> None:
        metric = tokens.BarMetric
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        recording = self.kind == "record"
        if self._active:
            fill = _colour("REC_ON_BG" if recording else "KIND_ON_BG")
            foreground = _colour("REC_ON_FG" if recording else "KIND_ON_FG")
        else:
            fill = _colour("KIND_HOVER_BG") if self._hovered else None
            foreground = _colour("FLAG_OFF_FG")
        rect = QRectF(self.rect())
        if fill is not None:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(fill)
            painter.drawRoundedRect(rect, metric.WELL_BTN_RADIUS, metric.WELL_BTN_RADIUS)
        offset = (metric.BTN - metric.ICON) / 2
        _draw_icon(
            painter,
            "camcorder" if recording else "camera",
            foreground,
            offset,
            offset,
            metric.ICON,
        )
        painter.end()


class _Well(QWidget):
    """A recessed group: the kind pair, and the two flags.

    Deliberate, per the handoff: inside a well an unlit icon reads as off,
    where a bare unlit icon reads as a button nobody has pressed yet.
    Presses on its padding are not swallowed here; they reach the row, which
    swallows them.
    """

    def __init__(self, *children: QWidget, parent=None):
        super().__init__(parent)
        metric = tokens.BarMetric
        layout = QHBoxLayout(self)
        layout.setContentsMargins(
            metric.WELL_PAD, metric.WELL_PAD, metric.WELL_PAD, metric.WELL_PAD
        )
        layout.setSpacing(metric.WELL_GAP)
        layout.setSizeConstraint(QLayout.SizeConstraint.SetFixedSize)
        for child in children:
            layout.addWidget(child)

    def paintEvent(self, event) -> None:
        radius = tokens.BarMetric.WELL_RADIUS
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(_colour("WELL_BG"))
        painter.drawRoundedRect(QRectF(self.rect()), radius, radius)
        painter.end()


class _ModeChip(_Surface):
    """Mode: the row's only labelled control, because it is the only
    decision re-made every snip. The active mode's glyph in the soft
    accent, its name, and a chevron for the menu it opens.
    """

    clicked = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._glyph = "crop"
        self._label = ""
        self._open = False
        self.setFixedHeight(tokens.BarMetric.BTN)
        self.setToolTip(tokens.MODE_CHIP_TOOLTIP)

    @property
    def label(self) -> str:
        return self._label

    @property
    def glyph(self) -> str:
        return self._glyph

    def set_content(self, glyph: str, label: str) -> None:
        metric = tokens.BarMetric
        self._glyph, self._label = glyph, label
        self.setFixedWidth(
            2 * metric.BORDER + metric.CHIP_PAD_L + metric.CHIP_ICON + metric.CHIP_GAP
            + _advance(label, tokens.BarFont.CHIP)
            + metric.CHIP_GAP + metric.CHEVRON + metric.CHIP_PAD_R
        )
        self.update()

    def is_open(self) -> bool:
        return self._open

    def set_open(self, is_open: bool) -> None:
        self._open = bool(is_open)
        self.update()

    def mouseReleaseEvent(self, event) -> None:
        if self._released_inside(event):
            self.clicked.emit()

    def paintEvent(self, event) -> None:
        metric = tokens.BarMetric
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        if self._open or self._hovered:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(_colour("ROW_HOVER_BG"))
            painter.drawRoundedRect(rect, metric.BTN_RADIUS, metric.BTN_RADIUS)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        # The open chip is visibly the source of the menu under it.
        painter.setPen(_colour("CHIP_BORDER_OPEN" if self._open else "BAR_BORDER"))
        painter.drawRoundedRect(rect, metric.BTN_RADIUS, metric.BTN_RADIUS)

        x = metric.BORDER + metric.CHIP_PAD_L
        _draw_icon(
            painter, self._glyph, _colour("ACCENT_SOFT"),
            x, (self.height() - metric.CHIP_ICON) / 2, metric.CHIP_ICON,
        )
        x += metric.CHIP_ICON + metric.CHIP_GAP
        painter.setFont(_font(tokens.BarFont.CHIP))
        painter.setPen(_colour("CHIP_FG"))
        painter.drawText(
            QRectF(x, 0, self.width() - x, self.height()),
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            self._label,
        )
        _draw_icon(
            painter, "chevron", _colour("ROW_NOTE_FG"),
            self.width() - metric.BORDER - metric.CHIP_PAD_R - metric.CHEVRON,
            (self.height() - metric.CHEVRON) / 2, metric.CHEVRON,
        )
        painter.end()


class _Divider(QWidget):
    """The rule between what to capture and what happens to it."""

    def __init__(self, parent=None):
        super().__init__(parent)
        metric = tokens.BarMetric
        self.setFixedSize(2 * metric.DIVIDER_MARGIN + 1, metric.BTN)

    def paintEvent(self, event) -> None:
        metric = tokens.BarMetric
        painter = QPainter(self)
        painter.fillRect(
            QRectF(metric.DIVIDER_MARGIN, (self.height() - metric.DIVIDER_H) / 2, 1, metric.DIVIDER_H),
            _colour("DIVIDER"),
        )
        painter.end()


class _DestinationButton(_Surface):
    """What happens after the capture: an icon, no label. A click cycles to
    the next destination and the tooltip names the current one. The stills
    bar's split button restates it a moment later, so the row need not.
    """

    clicked = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._glyph = "copy"
        self.setFixedSize(tokens.BarMetric.BTN, tokens.BarMetric.BTN)

    @property
    def glyph(self) -> str:
        return self._glyph

    def set_glyph(self, glyph: str) -> None:
        self._glyph = glyph
        self.update()

    def mouseReleaseEvent(self, event) -> None:
        if self._released_inside(event):
            self.clicked.emit()

    def paintEvent(self, event) -> None:
        metric = tokens.BarMetric
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        if self._hovered:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(_colour("CONTROL_HOVER_BG"))
            painter.drawRoundedRect(rect, metric.BTN_RADIUS, metric.BTN_RADIUS)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(_colour("BAR_BORDER"))
        painter.drawRoundedRect(rect, metric.BTN_RADIUS, metric.BTN_RADIUS)
        offset = (metric.BTN - metric.ICON) / 2
        _draw_icon(
            painter, self._glyph,
            _colour("CONTROL_HOVER_FG" if self._hovered else "TOOL_IDLE_FG"),
            offset, offset, metric.ICON,
        )
        painter.end()


class _Flag(_Surface):
    """A switch in the flag well.

    Armed is the soft accent on an 18% accent wash, so the row's one bright
    thing is always a live state. Unavailable is greyed and inert, and still
    hoverable, so why it cannot be used has somewhere to be read.
    """

    def __init__(self, glyph: str, parent=None):
        super().__init__(parent)
        self._glyph = glyph
        self._armed = False
        self._available = True
        self.setFixedHeight(tokens.BarMetric.BTN)

    def is_armed(self) -> bool:
        return self._armed

    def set_armed(self, armed: bool) -> None:
        self._armed = bool(armed)
        self.update()

    def is_available(self) -> bool:
        return self._available

    def set_available(self, available: bool) -> None:
        self._available = bool(available)
        self.setCursor(
            Qt.CursorShape.PointingHandCursor if self._available
            else Qt.CursorShape.ArrowCursor
        )
        self.update()

    def _foreground(self) -> QColor:
        if not self._available:
            return _colour("TOOL_DISABLED_FG")
        return _colour("ACCENT_SOFT" if self._armed else "FLAG_OFF_FG")

    def _paint_ground(self, painter: QPainter) -> None:
        if not self._available:
            return
        if self._armed:
            fill = _colour("ACCENT_WASH")
        elif self._hovered:
            fill = _colour("CONTROL_HOVER_BG")
        else:
            return
        radius = tokens.BarMetric.WELL_BTN_RADIUS
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(fill)
        painter.drawRoundedRect(QRectF(self.rect()), radius, radius)


class _HideFlag(_Flag):
    """Hide sensitive: black out passwords, keys and card numbers.

    The eye with a strike, never the droplet. The droplet is the blur tool's
    glyph and says "I will smudge this by hand"; this says the app finds and
    masks it.
    """

    toggled = pyqtSignal(bool)

    def __init__(self, parent=None):
        super().__init__(tokens.HIDE_SENSITIVE_GLYPH, parent)
        self.setFixedWidth(tokens.BarMetric.BTN)

    def mouseReleaseEvent(self, event) -> None:
        if self._available and self._released_inside(event):
            self._armed = not self._armed
            self.update()
            self.toggled.emit(self._armed)

    def paintEvent(self, event) -> None:
        metric = tokens.BarMetric
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        self._paint_ground(painter)
        offset = (metric.BTN - metric.ICON) / 2
        _draw_icon(painter, self._glyph, self._foreground(), offset, offset, metric.ICON)
        painter.end()


class _CursorFlag(_Flag):
    """Record the pointer, or leave it out. The record side's own flag,
    where Hide sensitive is the stills side's.

    On the row rather than only in Settings for the reason
    docs/design/pre-snip-chooser.md gives for the Last-region preference:
    "a preference nobody finds is a preference nobody has", and the row is
    where the decision is already being made. Settings keeps the switch --
    this writes the same stored value, so the two are never out of step.

    Greyed rather than hidden where the platform cannot honour it
    (`Platform.records_cursor`), carrying its reason: a control that
    silently does nothing is what Settings had, and it reads as the app
    being broken rather than the platform being limited.
    """

    toggled = pyqtSignal(bool)

    def __init__(self, parent=None):
        super().__init__(tokens.RECORD_CURSOR_GLYPH, parent)
        self.setFixedWidth(tokens.BarMetric.BTN)

    def mouseReleaseEvent(self, event) -> None:
        if self._available and self._released_inside(event):
            self._armed = not self._armed
            self.update()
            self.toggled.emit(self._armed)

    def paintEvent(self, event) -> None:
        metric = tokens.BarMetric
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        self._paint_ground(painter)
        offset = (metric.BTN - metric.ICON) / 2
        _draw_icon(painter, self._glyph, self._foreground(), offset, offset, metric.ICON)
        painter.end()


class _DelayFlag(_Flag):
    """Delay: a click advances through `tokens.DELAYS`. It shows its value
    only when one is set -- a countdown is the thing here that can surprise
    you, so it earns width exactly when it is armed.
    """

    clicked = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__("timer", parent)
        self._value = ""
        self.set_value("")

    @property
    def value(self) -> str:
        return self._value

    def set_value(self, value: str) -> None:
        metric = tokens.BarMetric
        self._value = value
        width = 2 * metric.FLAG_PAD_H + metric.ICON
        if value:
            width += metric.FLAG_GAP + _advance(value, tokens.BarFont.DELAY, mono=True)
        self.setFixedWidth(width)
        self.set_armed(bool(value))

    def mouseReleaseEvent(self, event) -> None:
        if self._released_inside(event):
            self.clicked.emit()

    def paintEvent(self, event) -> None:
        metric = tokens.BarMetric
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        self._paint_ground(painter)
        foreground = self._foreground()
        _draw_icon(
            painter, self._glyph, foreground,
            metric.FLAG_PAD_H, (self.height() - metric.ICON) / 2, metric.ICON,
        )
        if self._value:
            x = metric.FLAG_PAD_H + metric.ICON + metric.FLAG_GAP
            painter.setFont(_font(tokens.BarFont.DELAY, mono=True))
            painter.setPen(foreground)
            painter.drawText(
                QRectF(x, 0, self.width() - x, self.height()),
                int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                self._value,
            )
        painter.end()


_TEXT_LEFT = int(
    Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
) | int(Qt.TextFlag.TextDontClip)


class _RowSpec(NamedTuple):
    """One row of the mode menu.

    `subtitle` is Last region's dimensions or, for a row that cannot be
    picked, the reason -- one slot for both, so a greyed row explains itself
    where Last region says what it would restore. No row carries a note:
    what a mode does is its tooltip, and the hint pill once it is chosen.
    """

    value: str
    glyph: str
    label: str
    shortcut: str
    subtitle: str = ""
    subtitle_mono: bool = False
    tooltip: str = ""
    disabled: bool = False


class _MenuRow(_Surface):
    """Glyph, label, shortcut, tick -- and a subtitle under the label when
    the row has one. A disabled row is inert, not only greyed: it swallows
    its press (so the overlay does not start a drag) and never clicks.
    """

    clicked = pyqtSignal(str)

    def __init__(self, spec: _RowSpec, selected: bool = False, parent=None):
        super().__init__(parent, clickable=not spec.disabled)
        metric, font = tokens.BarMetric, tokens.BarFont
        self.spec = spec
        self._selected = bool(selected)
        self.setToolTip(spec.tooltip)
        pad_v, _pad_h = metric.MENU_ROW_PAD
        inner = metric.MENU_ROW_ICON
        if spec.subtitle:
            inner = max(
                inner,
                math.ceil(font.MENU_LABEL[0] + metric.MENU_NOTE_GAP + font.MENU_NOTE[0]),
            )
        self.setFixedHeight(2 * pad_v + inner)

    @property
    def value(self) -> str:
        return self.spec.value

    @property
    def selected(self) -> bool:
        return self._selected

    def set_selected(self, selected: bool) -> None:
        self._selected = bool(selected)
        self.update()

    def mouseReleaseEvent(self, event) -> None:
        if not self.spec.disabled and self._released_inside(event):
            self.clicked.emit(self.spec.value)

    def paintEvent(self, event) -> None:
        metric, font = tokens.BarMetric, tokens.BarFont
        spec = self.spec
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        width, height = self.width(), self.height()

        if not spec.disabled and (self._hovered or self._selected):
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(_colour("ROW_HOVER_BG" if self._hovered else "ROW_SELECTED_BG"))
            painter.drawRoundedRect(
                QRectF(self.rect()), metric.MENU_ROW_RADIUS, metric.MENU_ROW_RADIUS
            )

        if spec.disabled:
            foreground = _colour("TOOL_DISABLED_FG")
        else:
            foreground = _colour("ROW_SELECTED_FG" if self._selected else "ROW_IDLE_FG")

        _pad_v, pad_h = metric.MENU_ROW_PAD
        x = pad_h
        _draw_icon(
            painter, spec.glyph, foreground,
            x, (height - metric.MENU_ROW_ICON) / 2, metric.MENU_ROW_ICON,
        )
        x += metric.MENU_ROW_ICON + metric.MENU_ROW_GAP

        # The tick's slot is kept whether or not this row is the selected
        # one, so the shortcuts line up down the menu.
        right = width - pad_h - metric.MENU_TICK
        if self._selected and not spec.disabled:
            _draw_icon(
                painter, "check", _colour("ACCENT"),
                right, (height - metric.MENU_TICK) / 2, metric.MENU_TICK,
            )
        right -= metric.MENU_ROW_GAP
        if spec.shortcut:
            shortcut_w = _advance(spec.shortcut, font.MENU_SHORTCUT, mono=True)
            painter.setFont(_font(font.MENU_SHORTCUT, mono=True))
            # Dimmed with the rest of a disabled row: at full strength it
            # reads as if the key still did something.
            painter.setPen(_colour("TOOL_DISABLED_FG" if spec.disabled else "SHORTCUT_FG"))
            painter.drawText(
                QRectF(right - shortcut_w, 0, shortcut_w, height),
                int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                | int(Qt.TextFlag.TextDontClip),
                spec.shortcut,
            )
            right -= shortcut_w + metric.MENU_ROW_GAP
        column = max(0.0, right - x)

        # Elided rather than trusted: the menu is a fixed width, and a
        # fallback face can be wider than the one the width was tuned to.
        label_font = _font(font.MENU_LABEL)
        label = QFontMetricsF(label_font).elidedText(
            spec.label, Qt.TextElideMode.ElideRight, column
        )
        painter.setFont(label_font)
        painter.setPen(foreground)
        if not spec.subtitle:
            painter.drawText(QRectF(x, 0, column, height), _TEXT_LEFT, label)
            painter.end()
            return

        label_h, subtitle_h = font.MENU_LABEL[0], font.MENU_NOTE[0]
        top = (height - (label_h + metric.MENU_NOTE_GAP + subtitle_h)) / 2
        painter.drawText(QRectF(x, top, column, label_h), _TEXT_LEFT, label)
        subtitle_font = _font(font.MENU_NOTE, mono=spec.subtitle_mono)
        painter.setFont(subtitle_font)
        painter.setPen(_colour("ROW_NOTE_FG"))
        painter.drawText(
            QRectF(x, top + label_h + metric.MENU_NOTE_GAP, column, subtitle_h),
            _TEXT_LEFT,
            QFontMetricsF(subtitle_font).elidedText(
                spec.subtitle, Qt.TextElideMode.ElideRight, column
            ),
        )
        painter.end()


class _MenuRule(QWidget):
    """The rule between the capture modes and Last region."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(1 + 2 * tokens.BarMetric.MENU_RULE_MARGIN)

    def paintEvent(self, event) -> None:
        margin = tokens.BarMetric.MENU_RULE_MARGIN
        painter = QPainter(self)
        painter.fillRect(QRectF(margin, margin, self.width() - 2 * margin, 1), _colour("MENU_RULE"))
        painter.end()


class _Menu(QWidget):
    """The mode menu: a frameless top-level `Qt.Popup`, never a child
    widget of the row.

    Top-level because an open menu has to paint over the hint pill beneath
    the row, which a child of the row cannot do; and a popup closes itself
    on a click anywhere else, which is the dismissal the handoff asks for.
    """

    picked = pyqtSignal(str)
    closed = pyqtSignal()

    def __init__(self, rows, last_region: _RowSpec, selected: str, parent=None):
        super().__init__(parent, Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint)
        metric = tokens.BarMetric
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        # Its own window, but still the row's child, which is how its glass
        # finds the overlay behind it.
        self.glass = glass.Glass(self)
        column = QVBoxLayout(self)
        column.setContentsMargins(
            metric.MENU_PAD, metric.MENU_PAD, metric.MENU_PAD, metric.MENU_PAD
        )
        column.setSpacing(0)
        self._rows: dict[str, _MenuRow] = {}
        for spec in rows:
            self._add(column, spec, selected)
        self.rule = _MenuRule(self)
        column.addWidget(self.rule)
        self._add(column, last_region, selected)
        self.setFixedWidth(metric.MENU_W_MODE)
        self.adjustSize()

    def _add(self, column: QVBoxLayout, spec: _RowSpec, selected: str) -> None:
        row = _MenuRow(spec, spec.value == selected, self)
        row.clicked.connect(self.picked)
        self._rows[spec.value] = row
        column.addWidget(row)

    def closeEvent(self, event) -> None:
        self.closed.emit()
        super().closeEvent(event)

    def paintEvent(self, event) -> None:
        metric = tokens.BarMetric
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        path = glass.rounded(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), metric.MENU_RADIUS)
        self.glass.paint(painter, path, _colour("MENU_BG"))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(_colour("MENU_BORDER"))
        painter.drawPath(path)
        painter.end()


class ChooserRow(_Surface):
    """The row: kind, mode, a divider, destination, flags.

    Square top corners, 12px bottom corners and no top border, so it hangs
    from the monitor's edge -- the visual claim that it belongs to this
    monitor. The fill is 94% alpha over a blur of the frame behind it
    (`snipux.glass`), and every control on it is opaque.

    A press anywhere on it, gaps included, is still a press on the row
    (`_Surface`), and its background keeps an ordinary arrow while the
    frozen frame around it is a crosshair.
    """

    def __init__(self, parent=None):
        super().__init__(parent, clickable=False)
        metric = tokens.BarMetric
        # Only matters where the row is a window of its own -- a test's, with
        # no host -- so that what is under its translucent fill shows.
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        layout = QHBoxLayout(self)
        edge = metric.BORDER + metric.PAD
        # Height is ROW_H and the controls centre in it. The wells are 32px,
        # which the handoff's 6 + 28 + 6 does not leave room for; centring
        # keeps the row at ROW_H and the 28px controls at its 6px padding.
        layout.setContentsMargins(edge, 0, edge, metric.BORDER)
        layout.setSpacing(metric.GAP)

        self.stills = _KindButton("stills")
        self.record = _KindButton("record")
        self.kind_well = _Well(self.stills, self.record, parent=self)
        self.mode_chip = _ModeChip(self)
        self.divider = _Divider(self)
        self.destination = _DestinationButton(self)
        self.hide_flag = _HideFlag()
        self.cursor_flag = _CursorFlag()
        self.delay_flag = _DelayFlag()
        self.flag_well = _Well(
            self.hide_flag, self.cursor_flag, self.delay_flag, parent=self
        )
        for widget in (
            self.kind_well, self.mode_chip, self.divider, self.destination, self.flag_well
        ):
            layout.addWidget(widget, 0, Qt.AlignmentFlag.AlignVCenter)

        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(metric.SHADOW_BLUR)
        shadow.setOffset(0, metric.SHADOW_DY)
        shadow.setColor(_colour("SHADOW"))
        self.setGraphicsEffect(shadow)
        self.refit()

    def refit(self) -> None:
        """Size the row to its controls, now.

        Synchronous on purpose: a control that changes width (the chip's
        label, Delay's value, Hide sensitive leaving on the record side)
        would otherwise resize the row only on Qt's next layout pass, and the
        row would be centred on its old width until then.
        """
        for well in (self.kind_well, self.flag_well):
            well.layout().activate()
        layout = self.layout()
        layout.invalidate()
        self.setFixedSize(layout.sizeHint().width(), tokens.BarMetric.ROW_H)
        layout.activate()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        _paint_docked(
            painter, self, tokens.BarMetric.RADIUS_DOCKED,
            _colour("BAR_BG"), _colour("BAR_BORDER"),
        )
        painter.end()


class _HintPill(QWidget):
    """The hint line under the row: the active mode's glyph and its next
    step, or what the hovered control does.

    Passive: nothing on it clicks, so a press on it is left to the drag
    underneath rather than swallowed.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        # A plain widget, not a `_Surface`, so it builds its own.
        self.glass = glass.Glass(self)
        metric = tokens.BarMetric
        pad_v, _pad_h = metric.HINT_PAD
        self._glyph = "crop"
        self._text = ""
        self.setFixedHeight(2 * metric.BORDER + 2 * pad_v + metric.HINT_ICON)

    @property
    def glyph(self) -> str:
        return self._glyph

    @property
    def text(self) -> str:
        return self._text

    def set_content(self, glyph: str, text: str) -> None:
        metric = tokens.BarMetric
        _pad_v, pad_h = metric.HINT_PAD
        self._glyph, self._text = glyph, text
        self.setFixedWidth(
            2 * metric.BORDER + 2 * pad_h + metric.HINT_ICON + metric.HINT_ICON_GAP
            + _advance(text, tokens.BarFont.HINT)
        )
        self.update()

    def paintEvent(self, event) -> None:
        metric = tokens.BarMetric
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        path = glass.rounded(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), metric.HINT_RADIUS)
        self.glass.paint(painter, path, _colour("HINT_BG"))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(_colour("HINT_BORDER"))
        painter.drawPath(path)
        _pad_v, pad_h = metric.HINT_PAD
        x = metric.BORDER + pad_h
        _draw_icon(
            painter, self._glyph, _colour("ACCENT_SOFT"),
            x, (self.height() - metric.HINT_ICON) / 2, metric.HINT_ICON,
        )
        x += metric.HINT_ICON + metric.HINT_ICON_GAP
        painter.setFont(_font(tokens.BarFont.HINT))
        painter.setPen(_colour("HINT_FG"))
        painter.drawText(QRectF(x, 0, self.width() - x, self.height()), _TEXT_LEFT, self._text)
        painter.end()


class _Tab(_Surface):
    """What the row collapses to once a selection exists.

    22px on the same edge, carrying the mode, `then <destination>` and --
    when Hide sensitive is on -- the eye with a strike: flags carry through
    the change of stage, and keeping them visible is the point of the tab.

    Its 70% is the one real opacity on this surface, and correct here where
    it would be wrong on the row: the whole tab is meant to recede until the
    pointer is on it.
    """

    clicked = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._glyph = "crop"
        self._mode = ""
        self._tail = ""
        self._hide = False
        self.setFixedHeight(tokens.BarMetric.TAB_H)
        self.setToolTip(tokens.TAB_TOOLTIP)
        self._effect = QGraphicsOpacityEffect(self)
        self._effect.setOpacity(tokens.BarMetric.TAB_OPACITY)
        self.setGraphicsEffect(self._effect)

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def tail(self) -> str:
        return self._tail

    @property
    def carries_hide_sensitive(self) -> bool:
        return self._hide

    def opacity(self) -> float:
        return self._effect.opacity()

    def set_content(self, glyph: str, mode: str, tail: str, hide: bool) -> None:
        metric, font = tokens.BarMetric, tokens.BarFont
        self._glyph, self._mode, self._tail, self._hide = glyph, mode, tail, bool(hide)
        width = (
            2 * metric.BORDER + 2 * metric.TAB_PAD_H
            + metric.TAB_ICON + metric.TAB_ICON_GAP + _advance(mode, font.TAB_MODE)
            + 2 * metric.TAB_GAP + 1 + _advance(tail, font.TAB_TAIL)
        )
        if self._hide:
            width += 2 * metric.TAB_GAP + 1 + metric.TAB_ICON
        self.setFixedWidth(width)
        self.update()

    def enterEvent(self, event) -> None:
        self._effect.setOpacity(1.0)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._effect.setOpacity(tokens.BarMetric.TAB_OPACITY)
        super().leaveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if self._released_inside(event):
            self.clicked.emit()

    def paintEvent(self, event) -> None:
        metric, font = tokens.BarMetric, tokens.BarFont
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        _paint_docked(painter, self, metric.TAB_RADIUS, _colour("TAB_BG"), _colour("BAR_BORDER"))
        height = self.height()
        accent = _colour("ACCENT_SOFT")

        def separator(at: float) -> float:
            at += metric.TAB_GAP
            painter.fillRect(
                QRectF(at, (height - metric.TAB_SEP_H) / 2, 1, metric.TAB_SEP_H),
                _colour("TAB_SEP"),
            )
            return at + 1 + metric.TAB_GAP

        x = metric.BORDER + metric.TAB_PAD_H
        _draw_icon(painter, self._glyph, accent, x, (height - metric.TAB_ICON) / 2, metric.TAB_ICON)
        x += metric.TAB_ICON + metric.TAB_ICON_GAP
        painter.setFont(_font(font.TAB_MODE))
        painter.setPen(accent)
        painter.drawText(QRectF(x, 0, self.width() - x, height), _TEXT_LEFT, self._mode)
        x = separator(x + _advance(self._mode, font.TAB_MODE))
        painter.setFont(_font(font.TAB_TAIL))
        painter.setPen(_colour("ROW_NOTE_FG"))
        painter.drawText(QRectF(x, 0, self.width() - x, height), _TEXT_LEFT, self._tail)
        if self._hide:
            x = separator(x + _advance(self._tail, font.TAB_TAIL))
            _draw_icon(
                painter, tokens.HIDE_SENSITIVE_GLYPH, accent,
                x, (height - metric.TAB_ICON) / 2, metric.TAB_ICON,
            )
        painter.end()


_MODE_GLYPHS = {
    **{label: glyph for label, glyph, _note in tokens.CAPTURE_MODES},
    tokens.LAST_REGION_MODE: tokens.LAST_REGION_GLYPH,
}


class Chooser(QWidget):
    """The whole surface and its state machine.

    Two phases. **choosing**: the row and its hint pill. **collapsed**: the
    tab, once a selection exists. `OverlayWindow` moves between them
    (`collapse`, `reopen`), because only it knows when a selection exists;
    picking a mode never changes the phase, because picking a mode does not
    arm it.

    Everything positions against `set_screen`'s rect, the monitor the
    overlay hands over -- never the virtual desktop, which on a staggered
    desk puts the row in a gap between screens.
    """

    modeChosen = pyqtSignal(str)
    fireImmediately = pyqtSignal(str)
    cancelled = pyqtSignal()
    kindChanged = pyqtSignal(str)
    afterChanged = pyqtSignal(str)
    delayChanged = pyqtSignal(str)
    reuseLastRegionChanged = pyqtSignal(bool)
    hideSensitiveChanged = pyqtSignal(bool)
    # The record side's equivalent: whether the pointer is filmed.
    recordCursorChanged = pyqtSignal(bool)

    def __init__(self, parent=None, *, screen_rect: QRectF | None = None, origin=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

        self._mode = tokens.CAPTURE_MODES[0][0]
        self._after = tokens.AFTER_DEFAULT
        # What the record side falls back to when the chooser switches to
        # it. Seeded by `OverlayWindow` from Settings; a token default keeps a
        # Chooser built without one (every test that does) as it was.
        self._record_after_default = tokens.RECORD_AFTER_DEFAULT
        self._delay = tokens.DELAY_DEFAULT
        self._kind = "stills"
        # Whether there is a browser page to capture. False until told
        # otherwise, so a Chooser built without a seed greys the row rather
        # than promising a window nothing has found.
        self._browser_available = False
        # Why Active window cannot be picked, or None once `OverlayWindow`
        # has found the window the user was in.
        self._active_window_reason: str | None = tokens.ACTIVE_WINDOW_UNAVAILABLE
        # How many monitors the host window covers. One until told
        # otherwise, so Full screen fires on the pick (#53).
        self._monitor_count = 1
        # What Last region would restore, or why it cannot. Nothing until
        # `OverlayWindow` says what was captured last.
        self._last_region_size: QSizeF | None = None
        self._last_region_reason: str | None = tokens.LAST_REGION_NONE
        self._hide_sensitive_reason = ""
        # Each side's own destination, remembered across flips of the kind.
        self._after_by_kind: dict[str, str] = {}
        self._phase = "choosing"
        self._menu: _Menu | None = None
        # See MenuReopenGuard: without it, clicking the chip to close
        # this menu reopens it, because the press that dismissed the
        # popup reaches the chip only once the popup has gone.
        self._menu_guard = MenuReopenGuard()
        # The control whose explanation the hint pill is borrowed for.
        self._explaining: QWidget | None = None

        self.row = ChooserRow(parent)
        self.hint = _HintPill(parent)
        self.tab = _Tab(parent)
        row = self.row
        row.stills.picked.connect(self.set_kind)
        row.record.picked.connect(self.set_kind)
        row.mode_chip.clicked.connect(self._toggle_menu)
        row.destination.clicked.connect(self.cycle_after)
        row.hide_flag.toggled.connect(self._on_hide_toggled)
        row.cursor_flag.toggled.connect(self._on_cursor_toggled)
        row.delay_flag.clicked.connect(self.cycle_delay)
        self.tab.clicked.connect(self.reopen)
        for control in (
            row.stills, row.record, row.destination,
            row.hide_flag, row.cursor_flag, row.delay_flag,
        ):
            control.hovered.connect(self._on_control_hovered)

        self._record_cursor_reason = ""
        self._screen_rect = screen_rect
        self._origin = origin or QPoint(0, 0)
        self._refresh()

    # -- state -----------------------------------------------------------

    @property
    def record_cursor(self) -> bool:
        """Whether the pointer will be filmed."""
        return self.row.cursor_flag.is_armed()

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

    @property
    def reuse_last_region(self) -> bool:
        """Whether this snip is on Last region -- what the stored
        reuse-last-region preference means now it is a mode."""
        return self._mode == tokens.LAST_REGION_MODE

    @property
    def hide_sensitive(self) -> bool:
        return self.row.hide_flag.is_armed()

    @property
    def hide_sensitive_available(self) -> bool:
        return self.row.hide_flag.is_available()

    def set_mode(self, mode: str, *, announce: bool = True) -> None:
        """Adopt `mode`, and by default announce it. Never arms it.

        Announcing is how the overlay hears a pick: `fireImmediately` for a
        mode with nothing left to aim at, `modeChosen` for the rest. Neither
        collapses the row. It stays up until the user drags or clicks a
        window, and the overlay collapses it once a selection exists.

        `announce=False` adopts a value another surface already acted on --
        the floating bar's mode popover -- without sending it straight back.

        A pick onto or off Last region is also `reuseLastRegionChanged`,
        because that is what the next snip opens on (`set_reuse_last_region`).
        """
        if mode not in _MODE_GLYPHS:
            return
        if self._unavailable_reason(mode) is not None:
            # A greyed row already ignores its click; a stray shortcut key
            # has to leave the mode alone the same way.
            return
        was_last_region = self._mode == tokens.LAST_REGION_MODE
        self._mode = mode
        self._refresh()
        if not announce:
            return
        is_last_region = mode == tokens.LAST_REGION_MODE
        if is_last_region != was_last_region:
            self.reuseLastRegionChanged.emit(is_last_region)
        if self._fires_immediately(mode):
            self.fireImmediately.emit(mode)
        else:
            self.modeChosen.emit(mode)

    def set_browser_available(self, available: bool) -> None:
        """Say whether Browser has a page to capture.

        Set from outside rather than looked up here: this widget is Qt and
        `tokens` only -- no platform calls -- which is what lets its tests
        decide the answer instead of inheriting whatever is open.
        """
        self._browser_available = bool(available)
        self._refresh()

    def set_active_window_available(
        self, available: bool, reason: str = tokens.ACTIVE_WINDOW_UNAVAILABLE
    ) -> None:
        """Say whether Active window has a window to take, and if not, why.
        Only the caller knows which reason is true: the platform cannot name
        a focused window at all, or it can and found nothing.
        """
        self._active_window_reason = None if available else reason
        self._refresh()

    def set_monitor_count(self, count: int) -> None:
        """Say how many monitors the host window covers, which decides
        whether a monitor mode fires on the pick and what the hint says."""
        self._monitor_count = max(1, int(count))
        self._refresh()

    def set_last_region(
        self, size: QSizeF | None, reason: str = tokens.LAST_REGION_NONE
    ) -> None:
        """Say what Last region would restore: the logical size of the
        previous capture's rectangle as it lands on this desk, or None and
        why not.

        Set from outside, like `set_browser_available`: the overlay knows
        what was stored and which monitors the frame covers. A Last region
        that stops being possible while chosen falls back to Region.
        """
        self._last_region_size = None if size is None else QSizeF(size)
        self._last_region_reason = None if size is not None else reason
        if size is None and self._mode == tokens.LAST_REGION_MODE:
            self._mode = tokens.CAPTURE_MODES[0][0]
        self._refresh()

    def set_reuse_last_region(self, on: bool) -> None:
        """Seed whether this snip opens on Last region. Never emits.

        The stored preference predates Last region being a mode: it was a
        toggle on the row meaning "open on the last region". Picking the mode
        is that same choice now, so someone who had it on still opens on
        their region, and picking another mode turns it off.

        Stills only, as the toggle was: opening a recording on a rectangle
        would arm the recording. Seed the kind and `set_last_region` first;
        with nothing to restore the row is greyed and Region stands.
        """
        if on:
            if self._kind == "stills" and self._unavailable_reason(tokens.LAST_REGION_MODE) is None:
                self._mode = tokens.LAST_REGION_MODE
        elif self._mode == tokens.LAST_REGION_MODE:
            self._mode = tokens.CAPTURE_MODES[0][0]
        self._refresh()

    def _fires_immediately(self, mode: str) -> bool:
        """Whether choosing `mode` *is* the capture.

        Only on the stills side: on the record side every mode announces as
        `modeChosen` and arms the ready stage, where nothing is filmed until
        Record. Last region always has its rectangle. A monitor mode has
        nothing left to aim at only while there is one monitor; with more,
        which one is still the choice, so it arms and follows the pointer
        (#53).
        """
        if self._kind != "stills":
            return False
        if mode == tokens.LAST_REGION_MODE:
            return True
        if mode not in tokens.IMMEDIATE_MODES:
            return False
        return not (mode in tokens.MONITOR_MODES and self._monitor_count > 1)

    def _unavailable_reason(self, mode: str) -> "str | None":
        """Why `mode` cannot be picked right now, or None if it can.

        One place, so the menu's greyed rows and `set_mode`'s shortcut guard
        can never disagree about which modes are live.
        """
        if self._kind == "record" and mode in tokens.RECORD_DISABLED_MODES:
            return tokens.RECORD_DISABLED_MODES[mode]
        if mode == tokens.BROWSER_MODE and not self._browser_available:
            return tokens.BROWSER_UNAVAILABLE
        if mode == tokens.ACTIVE_WINDOW_MODE:
            return self._active_window_reason
        if mode == tokens.LAST_REGION_MODE:
            return self._last_region_reason
        return None

    def set_hide_sensitive(self, on: bool) -> None:
        """Seed Hide sensitive from stored config. Never emits."""
        self.row.hide_flag.set_armed(on)
        self._refresh()

    def set_record_cursor(self, on: bool) -> None:
        """Seed the pointer flag from stored config. Never emits -- the
        same split every other control here keeps between being told and
        being clicked."""
        self.row.cursor_flag.set_armed(on)
        self._refresh()

    def set_record_cursor_available(self, available: bool, reason: str = "") -> None:
        """Say whether this platform can honour the flag at all. Greyed
        with a reason rather than hidden, for the same reason Hide
        sensitive is: a control that silently does nothing reads as the app
        being broken, where a greyed one with a sentence reads as a
        limit."""
        self._record_cursor_reason = reason
        self.row.cursor_flag.set_available(available)
        self._refresh()

    def set_hide_sensitive_available(self, available: bool, reason: str = "") -> None:
        """Say whether this machine can read text out of a capture. Greyed
        with a reason rather than hidden, so a missing feature is not taken
        for a broken one."""
        self._hide_sensitive_reason = "" if available else reason
        self.row.hide_flag.set_available(available)
        self._refresh()

    def _on_hide_toggled(self, on: bool) -> None:
        self._refresh()
        self.hideSensitiveChanged.emit(on)

    def _on_cursor_toggled(self, on: bool) -> None:
        self._refresh()
        self.recordCursorChanged.emit(on)

    def set_record_after_default(self, after: str) -> None:
        """Seed what the record side opens on, from Settings. Applied on the
        next switch to the record side, or now if already on it."""
        if after not in {value for value, *_rest in _RECORD_AFTER_ROWS}:
            return
        self._record_after_default = after
        if self._kind == "record":
            self._after = after
            self._refresh()

    def set_after(self, after: str) -> None:
        """Adopt `after` as this snip's destination.

        Emits `afterChanged` on a real change only, which is what
        `OverlayWindow` persists from; seeding with the stored value is then
        never mistaken for the user choosing something.
        """
        if after == self._after:
            return
        self._after = after
        self._refresh()
        self.afterChanged.emit(after)

    def cycle_after(self) -> None:
        """The destination icon's click: the next of this side's
        destinations, wrapping."""
        values = [value for value, *_rest in self._destinations()]
        index = values.index(self._after) if self._after in values else -1
        self.set_after(values[(index + 1) % len(values)])

    def set_delay(self, delay: str) -> None:
        """Adopt `delay` as the countdown before this snip's grab.

        Emits `delayChanged` on a real change only. Without the signal the
        row's delay was a value nothing downstream ever read: the overlay
        kept its own copy, fed only by the floating bar's popover, so a
        delay picked here showed as set and the capture happened at once
        anyway (#73). The guard is what lets the overlay seed this back
        from the popover without the change bouncing between the two.
        """
        if delay not in tokens.DELAYS or delay == self._delay:
            return
        self._delay = delay
        self._refresh()
        self.delayChanged.emit(delay)

    def cycle_delay(self) -> None:
        """Delay's click: the next of `tokens.DELAYS`, wrapping to none."""
        delays = tokens.DELAYS
        self.set_delay(delays[(delays.index(self._delay) + 1) % len(delays)])

    def set_kind(self, kind: str) -> None:
        """Stills or record. Only `kind` changes, unless the current mode or
        destination means nothing on the new side, in which case it snaps
        to one that does -- assigned directly, so the snap never announces.

        Coming back to stills restores the destination that side had. The
        vocabularies overlap on `instant` and `save`, so a stills `edit`
        displaced on the way out used to stay displaced on the way back:
        flip twice and screenshots were silently Instant, which finishes the
        snip on the release of the drag. Reported as "sometimes it just
        instant captures when i drag the region".

        Emits `kindChanged` on every real flip, which is what persists it.
        """
        if kind not in ("stills", "record") or kind == self._kind:
            return
        leaving = self._kind
        self._kind = kind
        if kind == "record" and self._mode in tokens.RECORD_DISABLED_MODES:
            self._mode = tokens.CAPTURE_MODES[0][0]
        # Remembered and restored unconditionally, never gated on the
        # current value being illegal on the new side -- see the docstring.
        self._after_by_kind[leaving] = self._after
        remembered = self._after_by_kind.get(kind)
        if remembered is not None and self._valid_after(kind, remembered):
            self._after = remembered
        elif not self._valid_after(kind, self._after):
            self._after = (
                self._record_after_default if kind == "record" else tokens.AFTER_DEFAULT
            )
        self._refresh()
        self.kindChanged.emit(kind)

    @staticmethod
    def _valid_after(kind: str, after: str) -> bool:
        rows = _RECORD_AFTER_ROWS if kind == "record" else _AFTER_ROWS
        return after in {value for value, *_rest in rows}

    def _destinations(self):
        return _RECORD_AFTER_ROWS if self._kind == "record" else _AFTER_ROWS

    def collapse(self) -> None:
        """Down to the tab. The overlay's call, once a selection exists."""
        self._close_menu()
        self._phase = "collapsed"
        self._layout()

    def reopen(self) -> None:
        """Back to the row, with every choice as it was."""
        self._phase = "choosing"
        self._layout()

    # -- keyboard --------------------------------------------------------

    def handle_key(
        self, key: int, text: str, modifiers=Qt.KeyboardModifier.NoModifier
    ) -> bool:
        """Returns True if the key was the chooser's.

        The mode letters, Shift+R for Last region, Space to reopen the row
        from the tab, and Escape -- which closes the menu when one is open
        and otherwise cancels the snip.
        """
        if key == Qt.Key.Key_Escape:
            if self._close_menu():
                return True
            self.cancelled.emit()
            return True
        if key == Qt.Key.Key_Space and self._phase == "collapsed":
            self.reopen()
            return True
        letter = (text or "").upper()
        if letter == "R" and bool(modifiers & Qt.KeyboardModifier.ShiftModifier):
            self._close_menu()
            self.set_mode(tokens.LAST_REGION_MODE)
            return True
        mode = tokens.MODE_KEYS.get(letter)
        if mode is not None:
            self._close_menu()
            self.set_mode(mode)
            return True
        return False

    # -- the mode menu ---------------------------------------------------

    def _mode_rows(self) -> "tuple[list[_RowSpec], _RowSpec]":
        """The menu's capture modes, and Last region for under the rule."""
        keys = {mode: key for key, mode in tokens.MODE_KEYS.items()}
        recording = self._kind == "record"
        rows = []
        for label, glyph, note in tokens.CAPTURE_MODES:
            if recording:
                note = tokens.RECORD_MODE_NOTE.get(label, note)
            reason = self._unavailable_reason(label)
            rows.append(
                _RowSpec(
                    label, glyph, label, keys.get(label, ""),
                    subtitle=reason or "", tooltip=note, disabled=reason is not None,
                )
            )
        reason = self._unavailable_reason(tokens.LAST_REGION_MODE)
        subtitle = reason or ""
        if reason is None and self._last_region_size is not None:
            size = self._last_region_size
            subtitle = f"{round(size.width())} × {round(size.height())}"
        last_region = _RowSpec(
            tokens.LAST_REGION_MODE, tokens.LAST_REGION_GLYPH, tokens.LAST_REGION_MODE,
            tokens.LAST_REGION_SHORTCUT,
            subtitle=subtitle, subtitle_mono=reason is None,
            tooltip=tokens.LAST_REGION_NOTE, disabled=reason is not None,
        )
        return rows, last_region

    def _toggle_menu(self) -> None:
        if self._close_menu():
            return
        if self._menu_guard.blocks_reopen(self.row.mode_chip):
            return
        rows, last_region = self._mode_rows()
        menu = _Menu(rows, last_region, self._mode, self.row)
        # The press that closes the menu does nothing else, as in the spec:
        # replayed, a click on the chip would reopen the menu it just closed,
        # and a press on the frame would start a drag nobody meant.
        menu.setAttribute(Qt.WidgetAttribute.WA_NoMouseReplay, True)
        menu.picked.connect(self._on_picked)
        menu.closed.connect(self._on_menu_closed)
        self._menu = menu
        chip = self.row.mode_chip
        chip.set_open(True)
        menu.move(chip.mapToGlobal(QPoint(0, chip.height() + tokens.BarMetric.MENU_OFFSET)))
        menu.show()

    def _on_picked(self, value: str) -> None:
        self._close_menu()
        self.set_mode(value)

    def _on_menu_closed(self) -> None:
        # A popup also closes itself -- a click elsewhere, Escape -- and the
        # chip has to stop showing it as open when it does. When that click
        # was on the chip, the press is still on its way here, which is what
        # the guard is for.
        self._menu = None
        self._menu_guard.note_closed()
        self.row.mode_chip.set_open(False)

    def _close_menu(self) -> bool:
        if self._menu is None:
            return False
        menu, self._menu = self._menu, None
        menu.close()
        self.row.mode_chip.set_open(False)
        return True

    # -- what the row shows ----------------------------------------------

    def _next_step(self) -> str:
        next_step = tokens.MODE_NEXT_STEP.get(self._mode, "")
        if self._monitor_count > 1:
            next_step = tokens.MULTI_MONITOR_NEXT_STEP.get(self._mode, next_step)
        if self._kind == "record":
            next_step = tokens.RECORD_MODE_NEXT_STEP.get(self._mode, next_step)
        return next_step

    def _destination_tooltip(self) -> str:
        _glyph, label, note = _after_display(self._after, self._kind)
        return tokens.DESTINATION_TOOLTIP.format(label=label, note=note)

    def _hide_tooltip(self) -> str:
        if not self.hide_sensitive_available:
            return self._hide_sensitive_reason
        return tokens.HIDE_SENSITIVE_HINT[self.hide_sensitive]

    def _cursor_tooltip(self) -> str:
        if not self.row.cursor_flag.is_available():
            return self._record_cursor_reason
        return tokens.RECORD_CURSOR_HINT[self.row.cursor_flag.is_armed()]

    def _delay_tooltip(self) -> str:
        if self._delay == tokens.DELAY_DEFAULT:
            return tokens.DELAY_TOOLTIP_OFF
        return tokens.DELAY_TOOLTIP_ON.format(delay=self._delay)

    def _explanation(self, control: QWidget) -> "tuple[str, str]":
        """The hint pill's glyph and text while `control` is hovered."""
        row = self.row
        if control is row.stills or control is row.record:
            return ("camera" if control is row.stills else "record"), tokens.KIND_TOOLTIP[control.kind]
        if control is row.destination:
            return row.destination.glyph, self._destination_tooltip()
        if control is row.hide_flag:
            return tokens.HIDE_SENSITIVE_GLYPH, self._hide_tooltip()
        if control is row.cursor_flag:
            return tokens.RECORD_CURSOR_GLYPH, self._cursor_tooltip()
        return "timer", self._delay_tooltip()

    def _on_control_hovered(self, control: QWidget, hovered: bool) -> None:
        """Borrow the hint pill to say what a control does.

        The pill is the row's one line of prose, and the mode's next step
        is not urgent while the pointer is on something else. Tooltips are
        set too, but on an always-on-top frameless window they are a coin
        toss.
        """
        if hovered:
            self._explaining = control
        elif self._explaining is control:
            self._explaining = None
        self._refresh()

    def _refresh(self) -> None:
        row = self.row
        stills = self._kind == "stills"
        glyph = _MODE_GLYPHS[self._mode]
        row.stills.set_active(stills)
        row.record.set_active(not stills)
        row.mode_chip.set_content(glyph, self._mode)
        after_glyph, after_label, _note = _after_display(self._after, self._kind)
        row.destination.set_glyph(after_glyph)
        row.destination.setToolTip(self._destination_tooltip())
        # Stills only: recognition reads a frozen frame, and a recording has
        # no frozen frame to read.
        row.hide_flag.setVisible(stills)
        row.hide_flag.setToolTip(self._hide_tooltip())
        # The mirror of the line above: the pointer flag is a recording
        # question, and a still has no pointer in it to keep or drop.
        row.cursor_flag.setVisible(not stills)
        row.cursor_flag.setToolTip(self._cursor_tooltip())
        armed_delay = self._delay != tokens.DELAY_DEFAULT
        row.delay_flag.set_value(self._delay if armed_delay else "")
        row.delay_flag.setToolTip(self._delay_tooltip())
        row.refit()

        if self._explaining is not None:
            self.hint.set_content(*self._explanation(self._explaining))
        else:
            self.hint.set_content(glyph, self._next_step())
        self.tab.set_content(
            glyph, self._mode, f"then {after_label}",
            stills and self.hide_sensitive and self.hide_sensitive_available,
        )
        self._layout()

    # -- geometry --------------------------------------------------------

    def set_screen(self, screen_rect: QRectF, origin: QPoint) -> None:
        """`screen_rect` is the monitor to hang from, in absolute logical
        coordinates, already less whatever the desktop reserves on it;
        `origin` is the host window's top-left in the same space. A negative
        origin is ordinary -- a monitor left of or above the primary.
        """
        self._screen_rect, self._origin = QRectF(screen_rect), QPoint(origin)
        self._layout()

    def _layout(self) -> None:
        if self._screen_rect is None:
            return
        metric = tokens.BarMetric
        # Window-local logical: the monitor's absolute rect less the host
        # window's absolute origin.
        local = self._screen_rect.translated(-self._origin.x(), -self._origin.y())
        centre = local.x() + local.width() / 2
        top = local.y()
        choosing = self._phase == "choosing"

        self.row.move(round(centre - self.row.width() / 2), round(top))
        self.hint.move(
            round(centre - self.hint.width() / 2), round(top + metric.ROW_H + metric.HINT_GAP)
        )
        self.tab.move(round(centre - self.tab.width() / 2), round(top))
        self.row.setVisible(choosing)
        self.hint.setVisible(choosing)
        self.tab.setVisible(not choosing)
        if choosing:
            # The pill sits under the row; an open menu is its own window
            # and paints over both.
            self.hint.raise_()
            self.row.raise_()
        else:
            self.tab.raise_()

    def hide_all(self) -> None:
        self._close_menu()
        for widget in (self.row, self.hint, self.tab):
            widget.hide()


# `AFTER_CAPTURE` carries the ids and Settings' long prose; this binds each id
# to the glyph and short label the chooser draws, and the note its tooltip
# reads. The ids stay the shared spine, so a destination cannot exist on one
# surface and not the other.
_AFTER_ROWS = [
    ("instant", "copy", "Instant", tokens.CHOOSER_AFTER_NOTE["instant"]),
    ("edit", "pen", "Edit", tokens.CHOOSER_AFTER_NOTE["edit"]),
    ("save", "save", "Save", tokens.CHOOSER_AFTER_NOTE["save"]),
    ("review", "eye", "Review", tokens.CHOOSER_AFTER_NOTE["review"]),
]

# The record side's own vocabulary: Copy, Save, Open and GIF, never Edit or
# Review, because there is no annotate-in-place for a video. `instant` and
# `save` mean the same on both sides; `open` is the player, not the review
# window.
#
# GIF is offered here unconditionally, with no availability check: whether a
# system ffmpeg exists is a subprocess probe (`player.system_ffmpeg()`), and
# this row is built while the overlay is going up, on the one path snipux's
# own timing budget is measured against (see the timing test and #76). The
# player defers the same probe to its own menu open for exactly this reason.
# Settings' Recording pane is where a machine without one actually finds out
# -- opening it is a deliberate act, not part of a snip -- and a recording
# landed here without an encoder falls back to saving the plain file rather
# than losing the take (`AppController._land_recording_as_gif`).
_RECORD_AFTER_ROWS = [
    ("instant", "copy", "Copy", tokens.CHOOSER_RECORD_AFTER_NOTE["instant"]),
    ("save", "save", "Save", tokens.CHOOSER_RECORD_AFTER_NOTE["save"]),
    ("open", "pen", "Open", tokens.CHOOSER_RECORD_AFTER_NOTE["open"]),
    ("gif", "image", "GIF", tokens.CHOOSER_RECORD_AFTER_NOTE["gif"]),
]


def _after_display(identifier: str, kind: str = "stills") -> "tuple[str, str, str]":
    """(glyph, label, note) for a destination, or for this side's default
    when `identifier` is not one of its destinations."""
    rows = _RECORD_AFTER_ROWS if kind == "record" else _AFTER_ROWS
    for value, glyph, label, note in rows:
        if value == identifier:
            return glyph, label, note
    default = tokens.RECORD_AFTER_DEFAULT if kind == "record" else tokens.AFTER_DEFAULT
    return _after_display(default, kind)
