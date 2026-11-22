"""The popovers and menus that open off the floating bar: the style popover,
the capture-mode popover and the family menus.

Each is a small `_Chrome` surface that the bar's owner places by the
control that opened it -- `StylePopover` by the style dot,
`CaptureModePopover` by the capture chip, a `FamilyMenu` by a notched slot
-- and each reports a pick as a signal and decides nothing itself.

Separate from `toolbar.py` because a menu's rows, keys and sections change
without the bar changing, and separate from `overlay.py` because
`review.py` opens the same style popover and family menus over its own
canvas. Nothing in this module imports `overlay.py`.
"""

from __future__ import annotations

import math

from PyQt6.QtCore import QPointF, QRect, QRectF, QSize, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QFontMetricsF, QPainter, QPainterPath, QPen
from PyQt6.QtWidgets import (
    QColorDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from snipux import design, glass
from snipux.marks import ToolStyles
from snipux.toolbar import _Chrome


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
        # Keys belong to the overlay -- see _CloseButton.
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
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
        # Keys belong to the overlay -- see _CloseButton.
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
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


def _cycle_of(kind: str) -> list[tuple]:
    """The click-through cycle a `_CycleButton` of `kind` steps through."""
    return {
        "fill": design.tokens.FILL_CYCLE,
        "dash": design.tokens.DASH_CYCLE,
        "snap": design.tokens.SNAP_CYCLE,
    }[kind]


class _CycleButton(QPushButton):
    """Fill, line or snap: one click-through button that shows the state it is in.

    A click moves on to the next state, the way the chooser's destination and
    delay do -- one behaviour to learn, and what took the popover from three
    rows to two. The glyph is the state; the tooltip names it, and what a
    click changes it to.
    """

    def __init__(self, kind: str, parent=None):
        super().__init__(parent)
        metric = design.tokens.BarMetric
        self.kind = kind
        self._state = _cycle_of(kind)[0][0]
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
        elif self.kind == "snap":
            self._paint_snap_glyph(painter, rect.center())
        else:
            self._paint_dash_glyph(painter, rect.center())
        painter.end()

    def _paint_snap_glyph(self, painter: QPainter, centre: QPointF) -> None:
        """Two lines of type with a highlight fitted over the top one, or a
        loose freehand swash over them when the sweep is left as drawn.
        """
        metric = design.tokens.BarMetric
        glyph = design.bar_color("CYCLE_GLYPH")
        half = metric.SNAP_GLYPH_W / 2
        top_y = centre.y() - metric.SNAP_GLYPH_LINE_GAP / 2
        bottom_y = centre.y() + metric.SNAP_GLYPH_LINE_GAP / 2
        if self._state == "text":
            band = QRectF(0, 0, metric.SNAP_GLYPH_W + 2, metric.SNAP_GLYPH_BAND_H)
            band.moveCenter(QPointF(centre.x(), top_y))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(design.bar_color("CYCLE_GLYPH_WASH"))
            painter.drawRoundedRect(band, 2, 2)
        pen = QPen(glyph, metric.DASH_GLYPH_STROKE)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawLine(QPointF(centre.x() - half, top_y), QPointF(centre.x() + half, top_y))
        painter.drawLine(
            QPointF(centre.x() - half, bottom_y), QPointF(centre.x() + half * 0.4, bottom_y)
        )
        if self._state != "text":
            swash = QPainterPath(QPointF(centre.x() - half, top_y + 2))
            swash.cubicTo(
                QPointF(centre.x() - half * 0.3, top_y - 5),
                QPointF(centre.x() + half * 0.3, bottom_y + 3),
                QPointF(centre.x() + half, top_y - 1),
            )
            wash = QPen(design.bar_color("CYCLE_GLYPH_WASH"), metric.SNAP_GLYPH_BAND_H - 2)
            wash.setCapStyle(Qt.PenCapStyle.RoundCap)
            painter.setPen(wash)
            painter.drawPath(swash)

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

    SECTIONS = ("color", "fill", "dash", "snap", "strength", "size")

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
        self._snap_button = _CycleButton("snap", self._controls_row)
        self._snap_button.clicked.connect(lambda: self._cycle("snap"))
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
            self._snap_button,
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
            "snap": (self._snap_button,),
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
        self._snap_button.set_state(style.snap, self._cycle_tooltip("snap", style.snap))
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

        # Hiding a row, or a control inside one, changes the popover's
        # height, and Qt only settles a layout around a child shown or hidden
        # when it next gets round to it -- invalidating is not enough, the
        # cached sizes are still the last tool's. Activated inside out here,
        # so the height below is this tool's: the first open after switching
        # from the pen to a rectangle came up sized for the pen, with the fill
        # and line buttons cut off at the bottom.
        for row in (self._colour_row, self._controls_row):
            row.layout().activate()
        self._layout.activate()
        self.resize(self.width(), self.sizeHint().height())

    @staticmethod
    def _cycle_tooltip(kind: str, state: str) -> str:
        if kind == "fill":
            (_name, label), (_next, next_label) = _cycle_step(design.tokens.FILL_CYCLE, state)
            return f"Fill · {label} → click for {next_label}"
        if kind == "snap":
            (_name, label), (_next, next_label) = _cycle_step(design.tokens.SNAP_CYCLE, state)
            return f"{label} → click for {next_label}"
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
        _now, then = _cycle_step(_cycle_of(kind), getattr(self._styles.of(self._tool), kind))
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
        path = glass.rounded(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), metric.MENU_RADIUS)
        self.glass.paint(painter, path, design.bar_color("MENU_BG"))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(design.bar_color("MENU_BORDER"))
        painter.drawPath(path)
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
        # Keys belong to the overlay -- see _CloseButton.
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
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
        # Keys belong to the overlay -- see _CloseButton.
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
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
        self.glass.paint(painter, glass.rounded(rect, metric.MENU_RADIUS), bg)

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
        # Keys belong to the overlay -- see _CloseButton.
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
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
        path = glass.rounded(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), metric.MENU_RADIUS)
        self.glass.paint(painter, path, design.bar_color("MENU_BG"))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(design.bar_color("MENU_BORDER"))
        painter.drawPath(path)
        painter.end()
