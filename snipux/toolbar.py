"""The floating annotation bar: its buttons, the tool vocabulary they share,
and the hint strip that hangs under it.

`FloatingBar` is the row of tools and actions that sits by a selection in
the overlay and in the review window while it annotates; `ToolHintStrip`
names the active tool beneath it. Both are views: they report what was
clicked and the window that owns them decides what it means.

Separate from `overlay.py` because the bar is shared annotation chrome, not
part of the frozen-frame overlay -- `review.py` builds the same bar -- and
because it changes for its own reasons (a new tool, a new slot, a restyle)
that have nothing to do with selection or painting the frame. `_Chrome`,
the press-swallowing base every piece of overlay chrome is built on, lives
here too, so the popovers and the smaller chrome can build on it without
importing the overlay. Nothing in this module imports `overlay.py`.
"""

from __future__ import annotations

import math

from PyQt6.QtCore import QPoint, QPointF, QRect, QRectF, QSize, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QFontMetricsF, QIcon, QPainter, QPainterPath, QPen
from PyQt6.QtWidgets import QApplication, QHBoxLayout, QLabel, QPushButton, QWidget

from snipux import design, glass, setup_desktop
from snipux.marks import ToolStyle


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
    key that gets there. A tool with no key would be named alone rather
    than beside a dash leading nowhere.
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
    """The corner of a slot that opens that slot's menu.

    It means one thing -- this slot has more -- so only a slot with more
    carries one: the two families, and the watermark, whose more is where
    the mark goes and how strongly. A child of the slot rather than a hit
    test inside it: a press on the notch is delivered here, so it opens the
    menu and never also presses the slot underneath.

    It answers to more of the corner than the triangle covers (#77): the
    spec's 9px box was missed often enough that the menu seemed not to open
    at all. It stops short of the middle of the slot, so a click aimed at
    the glyph still reaches the slot.
    """

    clicked = pyqtSignal()

    def __init__(self, tooltip: str, parent: QWidget):
        super().__init__(parent)
        metric = design.tokens.BarMetric
        self.setFixedSize(metric.NOTCH, metric.NOTCH)
        corner = metric.BTN - metric.NOTCH
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
        metric = design.tokens.BarMetric
        leg = metric.NOTCH_TRIANGLE
        # Where the spec's markup lands its triangle: flush right and centred
        # top to bottom in a NOTCH_BOX box NOTCH_INSET in from the slot's
        # corner, the right angle pointing into the corner. The hit area
        # around that box is bigger, and the triangle does not grow with it.
        right = float(self.width() - metric.NOTCH_INSET)
        bottom = self.height() - metric.NOTCH_INSET - (metric.NOTCH_BOX - leg) / 2
        path = QPainterPath()
        path.moveTo(right, bottom - leg)
        path.lineTo(right, bottom)
        path.lineTo(right - leg, bottom)
        path.closeSubpath()
        painter.fillPath(path, self._fill())
        painter.end()

    def _fill(self) -> QColor:
        return design.bar_color("NOTCH_ACTIVE" if self._lit else "NOTCH_IDLE")


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
        # Keys belong to the overlay -- see _CloseButton.
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
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

        A family slot opens its menu from the notch, from a click once its
        sibling is armed, and from a right-click anywhere on it -- the way
        that menu was reached before the notch existed. `QPushButton`
        reports only left presses, hence this.
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

    def _colours(self, hovered: bool) -> "tuple[QColor | None, QColor]":
        """(background, or None for none, and glyph) for the state the
        button is in."""
        if not self.isEnabled():
            # Disabled is the preferred way to show an empty stack, over
            # just recolouring a still-live button -- so this is a real
            # QWidget.setEnabled(False), not a cosmetic-only state.
            return None, design.bar_color("TOOL_DISABLED_FG")
        if hovered and self._hover_color is not None:
            return self._hover_bg, self._hover_color
        if self._active:
            return design.bar_color("TOOL_ACTIVE_BG"), design.bar_color("TOOL_ACTIVE_FG")
        if hovered:
            return self._hover_bg, self._idle_color
        return None, self._idle_color

    def _refresh(self, hovered: bool | None = None) -> None:
        if hovered is None:
            hovered = self.underMouse()
        metric = design.tokens.BarMetric
        bg, glyph = self._colours(hovered)

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
        # Keys belong to the overlay -- see _CloseButton.
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
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

    Each also carries its `glass` (`snipux.glass`): the blurred crop of the
    frozen frame behind it, which a surface paints under its fill by
    calling `self.glass.paint` from its `paintEvent`. It lives here so that
    chrome built on this class has it without asking.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.glass = glass.Glass(self)

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


class _WatermarkNotch(_Notch):
    """The watermark slot's notch, lit in the slot's own colours: it
    follows the watermark being on, where a family's follows a tool being
    armed."""

    _available = True

    def set_available(self, available: bool) -> None:
        self._available = available
        self.setCursor(
            Qt.CursorShape.PointingHandCursor if available else Qt.CursorShape.ArrowCursor
        )
        self.update()

    def _fill(self) -> QColor:
        if not self._available:
            return design.bar_color("TOOL_DISABLED_FG")
        return design.watermark_color("NOTCH_ON" if self._lit else "NOTCH_OFF")


class _WatermarkSlot(_IconButton):
    """The watermark toggle, beside the style dot.

    Not a tool: nobody draws a watermark, so pressing it arms nothing. It
    switches the mark on or off, and its notch opens where the mark goes and
    how strongly. On, it wears the accent wash the handoff gives it rather
    than the white of an armed tool, so the bar never reads as having two
    tools armed at once.

    With nothing in Settings to stamp it stays on the bar, greyed, and its
    tooltip gives the reason. Greyed by its own state rather than by
    `setEnabled(False)`, the way the style dot dims for a tool with nothing
    to style, so the bar's two controls that can have nothing to do behave
    alike.
    """

    # Class defaults: `_IconButton.__init__` paints the slot before this
    # class's own `__init__` has had a chance to set them.
    _on = False
    _reason = ""

    def __init__(self, parent=None):
        super().__init__(
            design.tokens.WATERMARK["glyph"],
            design.tokens.WATERMARK_TOOLTIP_OFF,
            name="watermark",
            idle_color=design.watermark_color("OFF_FG"),
            parent=parent,
        )
        self._notch = _WatermarkNotch(design.tokens.WATERMARK_NOTCH_TOOLTIP, self)
        self._sync()

    @property
    def is_on(self) -> bool:
        return self._on

    @property
    def unavailable_reason(self) -> str:
        """Why there is nothing to stamp, or "" when there is."""
        return self._reason

    def set_on(self, on: bool) -> None:
        self._on = on
        self._sync()

    def set_unavailable_reason(self, reason: str) -> None:
        self._reason = reason
        self._sync()

    def _sync(self) -> None:
        available = not self._reason
        if not available:
            tooltip = self._reason
        elif self._on:
            tooltip = design.tokens.WATERMARK_TOOLTIP_ON
        else:
            tooltip = design.tokens.WATERMARK_TOOLTIP_OFF
        self.setToolTip(tooltip)
        self.setCursor(
            Qt.CursorShape.PointingHandCursor if available else Qt.CursorShape.ArrowCursor
        )
        self._notch.set_lit(self._on and available)
        self._notch.set_available(available)
        self._refresh()

    def _colours(self, hovered: bool) -> "tuple[QColor | None, QColor]":
        if self._reason:
            return None, design.bar_color("TOOL_DISABLED_FG")
        if self._on:
            # The glyph keeps the accent under the pointer, so hovering a
            # slot that is on never reads as it having switched off.
            bg = self._hover_bg if hovered else design.watermark_color("ON_BG")
            return bg, design.watermark_color("ON_FG")
        return (self._hover_bg if hovered else None), self._idle_color


class FloatingBar(_Chrome):
    """The stills bar: one row under the selection, per
    docs/design/bars/README.md section 2 -- the destination at the left end,
    eight tool slots in a fixed order of consequence, the style dot and the
    watermark, then undo and clear.

    Two of the slots hold families (`tokens.FAMILIES`): shapes and
    redaction. A family slot shows whichever sibling was used last, a notch
    in its corner asks for the family's menu, and each sibling keeps its own
    shortcut. The menu itself is the hosting window's, as is the popover the
    style dot opens: they are chrome over that window, not part of this row
    (see `FamilyMenu`).

    A real child widget of the window it sits over, built from real buttons
    -- never painted inside that window's paintEvent -- which is what gives
    every control a tooltip and hover state for free. `paintEvent` paints the
    glass -- the frame behind the bar blurred, under a translucent fill
    (`snipux.glass`) -- as a *brush*, not a reduced-*opacity* widget:
    `setWindowOpacity` would dim every glyph along with the background.

    The bar can be dragged by its own surface -- the padding round the row,
    the gaps between controls and the dividers, which is everywhere a press
    used to do nothing -- once `reposition` has placed it over a selection
    (#50, divergences.md 9). A press on a control is that control's, so a
    click can never turn into a drag. The review window lays its bar out
    itself and never calls `reposition`, so its bar does not drag: a drag
    has nothing to be clamped to until then.

    A selection that leaves the bar no room beside it keeps the bar on the
    selection's monitor, against its bottom margin (#79). On a desk with
    another monitor the host covers, a drag can carry it between the two
    (#50). `current_bounds` says which usable area it is in, so the host can
    keep what hangs off it there too.
    """

    # A drag of the bar itself crossed the drag threshold, and moved the
    # bar. What hangs off the bar is the host's, so it follows on these
    # rather than the bar reaching for it.
    dragStarted = pyqtSignal()
    dragMoved = pyqtSignal()
    # A drag ended somewhere the bar should go again the next time a
    # selection leaves it no room: which monitor, relative to the
    # selection's (`setup_desktop.BAR_ON_OWN_MONITOR` or
    # `BAR_ON_OTHER_MONITOR`), and the `spot` it ended on there, as (x, y).
    positionRemembered = pyqtSignal(str, float, float)

    toolSelected = pyqtSignal(str)
    # A tool picked with the pointer on this row, emitted just before the
    # `toolSelected` it causes. Clicking a tool closes whatever menu is open;
    # a key that changes the tool does not have to, and only this tells the
    # two apart.
    toolPicked = pyqtSignal(str)
    # "shapes" or "redact": the notch was pressed, or the slot right-clicked.
    familyMenuRequested = pyqtSignal(str)
    styleRequested = pyqtSignal()
    # The watermark slot pressed, or its notch -- only while there is a mark
    # to stamp. Whether the watermark is on is the hosting window's to say.
    watermarkToggled = pyqtSignal()
    watermarkMenuRequested = pyqtSignal()
    undoRequested = pyqtSignal()
    redoRequested = pyqtSignal()
    clearRequested = pyqtSignal()
    copyRequested = pyqtSignal()
    saveRequested = pyqtSignal()
    openRequested = pyqtSignal()
    # SNX-83: the destination menu's fourth ending, alongside the three
    # above -- see `OverlayWindow._open_destination_menu`.
    pinRequested = pyqtSignal()
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
    _FAMILY_TOOLTIP_TAILS = {
        "shapes": "click again for more shapes",
        "redact": "click again to switch",
    }

    def __init__(self, parent=None, *, capture_chip: bool = True, trailing: str = "save"):
        """`capture_chip` and `trailing` exist for the review window, which
        instantiates this very widget rather than growing a second one --
        see `docs/design/handoff-windows.md`, "Annotate mode".

        There is nothing left to capture in that window, so its bar has no
        capture-mode chip; and its footer already owns Copy and Save As, so
        its trailing action is `Done` instead. Those are the only two
        differences the design allows, which is why they are the only two
        parameters.

        The watermark slot goes with `Done`. The snip a review window opens
        is one the overlay has already exported, stamped with the watermark
        if it was on, so a slot there could only stamp it a second time.
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
        # The usable area of the monitor a drag can carry the bar to when
        # the selection leaves it no room on its own (#50), in the parent's
        # logical coordinates like `_last_bounds`, or None on a desk with no
        # such monitor -- and which of the two the bar is on now.
        self._last_elsewhere: QRectF | None = None
        self._monitor = setup_desktop.BAR_ON_OWN_MONITOR

        # A press on the bar's own surface that may become a drag (#50):
        # where it landed and where the bar's top-left was then, both in the
        # parent's logical coordinates. The bar's own coordinates move with
        # the bar, so a drag measured in them would chase itself. None while
        # no press is open.
        self._drag_press: QPointF | None = None
        self._drag_origin: QPoint | None = None
        self._dragging = False
        # Where a drag put the bar for the selection it was made over --
        # which monitor, and the `spot` on it -- kept until that selection
        # changes, so a re-sync for the same selection does not snap the
        # bar back.
        self._dragged: setup_desktop.BarPosition | None = None
        self._dragged_for: QRect | None = None
        # Where the bar goes when neither side of a selection has room, or
        # None for the automatic answer.
        self._remembered: setup_desktop.BarPosition | None = None

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

        self._style_dot = _StyleDot(self)
        self._style_dot.clicked.connect(self.styleRequested)
        layout.addWidget(self._style_dot)

        # The watermark shares the style dot's group, where the handoff puts
        # it: the two slots that set how the snip looks, rather than what is
        # drawn on it. The window it sits over decides what a press means.
        self._watermark = _WatermarkSlot(self)
        self._watermark.clicked.connect(self._on_watermark_pressed)
        self._watermark.notch.clicked.connect(self._on_watermark_menu_pressed)
        self._watermark.rightClicked.connect(self._on_watermark_menu_pressed)
        if self._trailing == "done":
            self._watermark.hide()
        else:
            layout.addWidget(self._watermark)
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
        icon = {"Copy": "copy", "Save": "save", "Open": "eye", "Pin": "pin"}.get(
            destination, "copy"
        )
        self._action.set_destination(destination, icon)

    def destination(self) -> str:
        return self._action.destination() if self._action is not None else "Copy"

    def _on_destination_activated(self, destination: str) -> None:
        {
            "Copy": self.copyRequested,
            "Save": self.saveRequested,
            "Open": self.openRequested,
            "Pin": self.pinRequested,
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
            self.reposition(self._last_selection, self._last_bounds, self._last_elsewhere)
        else:
            self.resize(self.sizeHint())

    # -- tools -------------------------------------------------------------

    def _on_slot_clicked(self, slot: str) -> None:
        """Arm the slot's tool: for a family, the sibling it is showing. A
        family slot whose sibling is already armed asks for the family's
        menu instead, so a second click opens it and a third closes it (#77).

        A click never moves on to the next sibling. The menu is how to reach
        another, and each has its key; a click that sometimes changed what
        the slot draws would make the one gesture used most the one that
        cannot be trusted. A click on an armed slot changed nothing at all,
        so opening the menu costs the first click none of its certainty --
        and the pointer is no longer sent to the notch alone to find it.
        No pick is reported either: a pick closes the bar's menus, this one
        included.
        """
        tool = self._family_choice.get(slot, slot)
        if slot in design.tokens.FAMILIES and tool == self._active_tool:
            self.familyMenuRequested.emit(slot)
            return
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

    # -- watermark ---------------------------------------------------------

    def set_watermark_on(self, on: bool) -> None:
        self._watermark.set_on(on)

    def set_watermark_unavailable(self, reason: str) -> None:
        """Grey the watermark slot with `reason` for why, or pass "" when
        there is a mark to stamp."""
        self._watermark.set_unavailable_reason(reason)

    def watermark_rect(self, host: QWidget) -> QRect:
        """The watermark slot, in `host`'s coordinates -- where its menu is
        anchored."""
        return QRect(self._watermark.mapTo(host, QPoint(0, 0)), self._watermark.size())

    def _on_watermark_pressed(self) -> None:
        if not self._watermark.unavailable_reason:
            self.watermarkToggled.emit()

    def _on_watermark_menu_pressed(self) -> None:
        if not self._watermark.unavailable_reason:
            self.watermarkMenuRequested.emit()

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
        path = glass.rounded(rect, metric.RADIUS)

        # design.bar_color("BAR_BG") already carries its 94% alpha --
        # painted here as a translucent *fill* over the blurred frame, never
        # as reduced *widget* opacity, so every child painted after this
        # stays fully opaque.
        self.glass.paint(painter, path, design.bar_color("BAR_BG"))

        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(design.bar_color("BAR_BORDER"))
        painter.drawPath(path)
        painter.end()

    # -- dragging (#50) ----------------------------------------------------

    @property
    def is_dragging(self) -> bool:
        """A press on the bar's own surface is open, whether or not it has
        moved far enough to be a drag yet.
        """
        return self._drag_press is not None

    def set_remembered_position(self, position: "setup_desktop.BarPosition | None") -> None:
        """Where the bar goes when a selection leaves it no room on either
        side -- which monitor, and the `spot` on it -- or None to leave that
        to `placement`.
        """
        self._remembered = position

    @property
    def current_bounds(self) -> "QRectF | None":
        """The usable area the bar is on now, in the parent's logical
        coordinates: the selection's own monitor's, or the other monitor's
        once a drag, or a place remembered from one, has put the bar there.
        None until `reposition` has placed it.

        What hangs off the bar is clamped into this. Clamped into the
        selection's monitor on behalf of a bar on the next one, a menu would
        open across the gap between the two, where nothing is drawn.
        """
        return self._bounds_on(self._monitor)

    def _bounds_on(self, monitor: str) -> "QRectF | None":
        if monitor == setup_desktop.BAR_ON_OTHER_MONITOR and self._last_elsewhere is not None:
            return self._last_elsewhere
        return self._last_bounds

    def _has_room(self) -> bool:
        """Whether the selection leaves a bar of this one's size room beside
        it on its own monitor. Only asked once `reposition` has run.
        """
        return (
            self._beside(QRectF(self._last_selection), self._last_bounds, self.size())
            is not None
        )

    def _monitor_under(self, pointer: QPointF) -> str:
        """Which monitor a drag with the pointer at `pointer` (the parent's
        logical coordinates) puts the bar on: the one whose usable area is
        nearest the pointer, or the one it is already on when both are as
        near.

        Nearest, not "the one under the pointer": the pointer can be on
        neither, in the gap beside a staggered monitor, over a dock, or on a
        third monitor the bar is never sent to. The bar is then clamped into
        whichever usable area is closest, so it is never drawn where no
        monitor shows it.

        The other monitor is a choice only while the selection leaves no
        room on its own. Beside a selection with room a drag moves the bar
        for that snip alone, and taking it to another monitor would send it
        away from a selection it can sit beside.
        """
        own = setup_desktop.BAR_ON_OWN_MONITOR
        if self._last_elsewhere is None or self._has_room():
            return own

        def distance(bounds: QRectF) -> float:
            across = max(bounds.left() - pointer.x(), 0.0, pointer.x() - bounds.right())
            down = max(bounds.top() - pointer.y(), 0.0, pointer.y() - bounds.bottom())
            return math.hypot(across, down)

        to_own = distance(self._last_bounds)
        to_other = distance(self._last_elsewhere)
        if to_own == to_other:
            return self._monitor
        return own if to_own < to_other else setup_desktop.BAR_ON_OTHER_MONITOR

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
        pointer = self.mapToParent(event.position())
        travel = pointer - self._drag_press
        if not self._dragging:
            # A press on the padding is usually a click that missed a
            # button by a pixel or two, and must leave the bar where it is.
            if travel.manhattanLength() < QApplication.startDragDistance():
                return
            self._dragging = True
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            self.dragStarted.emit()
        # The pointer picks the monitor and the bar is clamped inside it, so
        # carrying it over a bezel moves it across in one step rather than
        # letting it hang half over the gap.
        self._monitor = self._monitor_under(pointer)
        self.move(
            self.clamped(QPointF(self._drag_origin) + travel, self.current_bounds, self.size())
        )
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
        remembered (`positionRemembered`): the monitor it ended on, relative
        to the selection's, and the spot on it. From then on it is the
        answer whenever a selection leaves no room, over the placement at the
        foot of the selection's monitor, so the two never take turns. One
        made beside a selection with room is kept for that selection only:
        it moved the bar for this snip, and remembering it would change
        where the bar goes for a whole-monitor snip that it was never about.
        """
        dragged = self._dragging
        self._drag_press = None
        self._drag_origin = None
        self._dragging = False
        if not dragged or self._last_bounds is None or self._last_selection is None:
            return
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        monitor = (
            self._monitor if self._last_elsewhere is not None else setup_desktop.BAR_ON_OWN_MONITOR
        )
        position = setup_desktop.BarPosition(
            monitor, self.spot(QPointF(self.pos()), self._bounds_on(monitor), self.size())
        )
        self._dragged = position
        self._dragged_for = QRect(self._last_selection)
        if not self._has_room():
            self._remembered = position
            self.positionRemembered.emit(position.monitor, *position.spot)

    # -- positioning -----------------------------------------------------

    def reposition(
        self, selection: QRect, bounds: QRectF, elsewhere: "QRectF | None" = None
    ) -> None:
        """Put the bar where it belongs for `selection`: where a drag put it
        for this same selection, and otherwise where `placement` says.

        `bounds` is the usable area of the selection's own monitor.
        `elsewhere` is the usable area of the monitor a drag can carry the
        bar to when the selection leaves it no room there, or None on a desk
        with no such monitor. All three are in the parent's logical
        coordinates.

        Kept apart from `placement` so the rule and applying it stay two
        things: a position the user chose replaces the one, not the other.
        """
        if self._dragged_for is not None and QRect(selection) != self._dragged_for:
            self._dragged = None
            self._dragged_for = None
        # SNX-68: remembered so `set_capture_mode` can replay this same
        # call when the chip's width changes.
        self._last_selection = selection
        self._last_bounds = bounds
        self._last_elsewhere = elsewhere
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
        if self._dragged is not None:
            self._monitor, top_left = self.resolve(self._dragged, bounds, elsewhere, size)
        else:
            self._monitor, top_left = self.placement(
                QRectF(selection), bounds, size, self._remembered, elsewhere
            )
        self.setGeometry(QRect(top_left, size))

    @staticmethod
    def placement(
        selection: QRectF,
        bounds: QRectF,
        size: QSize,
        remembered: "setup_desktop.BarPosition | None" = None,
        elsewhere: "QRectF | None" = None,
    ) -> "tuple[str, QPoint]":
        """Where a bar of `size` belongs for `selection`: the monitor it is
        on (`setup_desktop.BAR_ON_OWN_MONITOR` or `BAR_ON_OTHER_MONITOR`)
        and its top-left, in the space `selection`, `bounds` and `elsewhere`
        share.

        `bounds` is the selection's own monitor, less whatever the desktop
        reserves on it (`OverlayWindow._chrome_bounds`) -- never the window.
        On a desk whose monitors are staggered vertically, clamping to the
        window put the bar for a selection low on a short monitor into the
        gap below it: inside the window, on no monitor at all.

        Centred on the selection, `BAR_OFFSET_Y` below it, and at least
        `BAR_EDGE_MARGIN` inside `bounds` on every side.

        Where there is no room below, the bar goes inside the foot of a
        tall selection, or above a short one rather than being clamped back
        up over it, as the handoff's `BAR_BOTTOM_ROOM` would put it
        (divergences.md 8). That clamp covers the very pixels the user
        framed in order to mark them up -- reported as "when u select a
        small region the controls are in the region so u cant edit
        anything", on a 1123x74 strip. See `_beside`.

        With room on none of those the selection is essentially the whole
        monitor, and anywhere on it covers some of it (#50). Then, in turn:

        - A `remembered` drag wins, on the monitor it names (`resolve`) --
          `elsewhere`, when that drag ended on another monitor. Were the
          placement below allowed to override it, the user's correction and
          the rule would take turns on every snip.
        - Otherwise the bar is held inside the foot of the selection, just
          clear of its bottom edge so the edge can still be dragged, and
          inside `bounds`.
          It once went to `elsewhere` on its own, just across the bezel
          (#50), and that put the controls a monitor away from the work they
          were for (#79). Only a drag takes it there now.

        None of that is consulted while there is room beside the selection,
        so neither a remembered place nor another monitor can take the bar
        away from a selection it could sit beside.
        """
        metric = design.tokens.BarMetric
        margin = metric.BAR_EDGE_MARGIN
        own = setup_desktop.BAR_ON_OWN_MONITOR
        left = selection.center().x() - size.width() / 2
        left = max(bounds.left() + margin, min(left, bounds.right() - margin - size.width()))
        top = FloatingBar._beside(selection, bounds, size)
        if top is not None:
            return own, QPoint(round(left), round(top))
        if remembered is not None:
            return FloatingBar.resolve(remembered, bounds, elsewhere, size)
        # Inside the selection's foot, clear of its bottom edge so that edge
        # can still be dragged -- or the monitor's foot, when the selection
        # runs on under the taskbar.
        top = min(bounds.bottom(), selection.bottom()) - margin - size.height()
        top = max(bounds.top() + margin, top)
        return own, QPoint(round(left), round(top))

    @staticmethod
    def resolve(
        position: "setup_desktop.BarPosition",
        bounds: QRectF,
        elsewhere: "QRectF | None",
        size: QSize,
    ) -> "tuple[str, QPoint]":
        """The monitor and top-left that a remembered or dragged `position`
        names for a bar of `size`, given the selection's own usable area
        `bounds` and the other monitor's `elsewhere`. Same space in and out.

        A position on the other monitor, on a desk that now has none, keeps
        its spot on the selection's own monitor: it is still the last place
        the user put the bar, and any place there covers something.
        """
        monitor, spot = position
        if monitor == setup_desktop.BAR_ON_OTHER_MONITOR and elsewhere is not None:
            return monitor, FloatingBar.from_spot(spot, elsewhere, size)
        return setup_desktop.BAR_ON_OWN_MONITOR, FloatingBar.from_spot(spot, bounds, size)

    # How many bar-heights tall a selection must be before the bar may sit
    # inside it: at three, the bar covers at most a third of it.
    _INSIDE_MIN_BARS = 3

    @staticmethod
    def _beside(selection: QRectF, bounds: QRectF, size: QSize) -> "float | None":
        """The top that puts a bar of `size` beside `selection` inside
        `bounds` -- below it, or above it when below does not fit and the
        selection is short -- or None otherwise.

        A tall selection never sends the bar above it: with no room below
        it reaches the bottom of the screen, and above it puts the bar far
        from where the user is working. It gets `placement`'s no-room
        answer instead -- inside its own foot, or where the user dragged
        the bar last time. Above is for a short strip, which the bar inside
        it would cover.
        """
        metric = design.tokens.BarMetric
        below = selection.bottom() + metric.BAR_OFFSET_Y
        if below <= bounds.bottom() - metric.BAR_EDGE_MARGIN - size.height():
            return below
        if selection.height() >= FloatingBar._INSIDE_MIN_BARS * size.height():
            return None
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
        self.glass.paint(painter, glass.rounded(QRectF(self.rect()), self._RADIUS), bg)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(design.color("BAR_BORDER"))
        painter.drawRoundedRect(
            QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), self._RADIUS, self._RADIUS
        )
        painter.end()
