"""The capture flow's post-selection bars (docs/design/flow, LOCKED).

The chooser lives in `chooser.py`; this is what replaces it once a selection
exists. One bar per kind -- the recording bar is here, the stills bar follows
-- and they never coexist, so they share the chrome below rather than each
growing their own.

Three rules from the handoff shape every widget here. Each was arrived at by
building the alternative and rejecting it, so breaking one undoes the design
rather than merely changing it:

1. **Every bar is centred on the selection.** Not edge-anchored. Placement is
   the caller's job (`app.py` owns the geometry), but the consequence is
   here: a centred bar that changes width moves *both* edges, which is why
2. **nothing collapses.** Every control that will ever be in a bar is built
   once and shown or hidden per state, never added and removed -- a bar that
   relaid itself between states would shift sideways under the cursor.
3. **The primary action sits at the LEFT end**, before a divider, and is the
   only accent-filled control. Picking anything to its right never changes
   it.

These are views, not controllers. A bar reports that a control was clicked
and `AppController` decides what that means, the same split the pill this
replaces already used -- which is what lets the audio menu, the destination menu and
the platform's own opinion about what is possible live in one place instead
of three.
"""

from __future__ import annotations

import math
import time

from PyQt6.QtCore import QPoint, QPointF, QRect, QRectF, QSize, Qt, pyqtSignal
from PyQt6.QtGui import (
    QColor,
    QCursor,
    QFont,
    QFontMetrics,
    QFontMetricsF,
    QGuiApplication,
    QPainter,
    QPainterPath,
    QPen,
    QRegion,
)
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QWidget

from snipux import design, glass
from snipux.design import tokens


def _font(size: float, weight: int, mono: bool = False) -> QFont:
    families = design.font_families()
    font = QFont(families.mono if mono else families.ui)
    font.setPixelSize(round(size))
    font.setWeight(QFont.Weight(weight))
    return font


def _glass(
    painter: QPainter, surface: "glass.Glass", rect: QRectF, radius: float, *, live: bool
) -> None:
    """The warm glass every bar is drawn on: `surface`'s blur of the frozen
    frame behind it under the fill, as the stills bar's is, or -- with no
    frame behind it to blur -- the denser fill `snipux.glass` falls back to.

    It used to be the 93% fill alone, on the grounds that the desktop behind
    was a frozen grab with nothing moving to give the missing blur away. But
    a still desktop has text in it, and 7% of a window title through the
    bar read as a ghost of some other control sitting behind it.

    Alpha, not opacity -- a translucent *fill* under fully opaque children.
    `setWindowOpacity(0.93)` would wash the glyphs out with it, which the
    handoff calls out by name.

    `live` swaps the hairline for red. Red appears nowhere else in the
    product, which is exactly what lets the border alone say "recording"
    without a label; do not spend it on anything that is not live.
    """
    path = glass.rounded(rect, radius)
    surface.paint(painter, path, design.flow_color("BAR_BG"))

    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.setPen(
        design.flow_color("BAR_BORDER_LIVE" if live else "BAR_BORDER")
    )
    painter.drawPath(path)


def _paint_notch(painter: QPainter, width: float, height: float, *, enabled: bool) -> None:
    """The stills bar's "this opens a menu" triangle, in a control's
    bottom-right corner -- the same geometry and colour as its `_Notch`, so
    the two bars say it the same way.
    """
    metric = tokens.BarMetric
    leg = metric.NOTCH_TRIANGLE
    right = float(width - metric.NOTCH_INSET)
    bottom = height - metric.NOTCH_INSET - (metric.NOTCH_BOX - leg) / 2
    path = QPainterPath()
    path.moveTo(right, bottom - leg)
    path.lineTo(right, bottom)
    path.lineTo(right - leg, bottom)
    path.closeSubpath()
    painter.fillPath(
        path, design.bar_color("NOTCH_IDLE" if enabled else "TOOL_DISABLED_FG")
    )


class _Divider(QWidget):
    """The hairline between a bar's control groups."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        # Hyprland's recording-control placement hook targets this exact
        # frameless window after the fullscreen selection overlay closes.
        # Keep it stable and regex-safe; it is not user-facing chrome.
        self.setWindowTitle("snipux-recording-controls")
        self.setFixedSize(1, tokens.FlowMetric.DIVIDER_H)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        # FlowMetric carries DIVIDER_H, not DIVIDER -- the colour lives on
        # FlowColor. `__dict__.get` does not search bases either, so this
        # always fell through to the hardcoded pair and only looked right by
        # coincidence: retune FlowColor.DIVIDER and every other divider in
        # the app would follow except this one.
        colour = design.color("DIVIDER")
        colour.setAlphaF(tokens.Color.DIVIDER_ALPHA)
        painter.fillRect(self.rect(), colour)
        painter.end()


class _IconButton(QWidget):
    """One 28px control: a glyph, a hover wash, and a click.

    Disabled is a *visible* state here rather than a hidden one, because the
    only thing that disables a control in this design is a platform that
    cannot do it -- Linux has no audio route at all -- and the handoff is
    explicit that the reason must be readable rather than the option
    vanishing.
    """

    clicked = pyqtSignal()

    def __init__(self, icon_name: str, parent: QWidget | None = None):
        super().__init__(parent)
        metric = tokens.FlowMetric
        self.setFixedSize(metric.BTN, metric.BTN)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._icon_name = icon_name
        self._hovered = False
        self._enabled = True
        self._active = False
        self._notch = False
        self._readout = False

    def set_icon(self, icon_name: str) -> None:
        self._icon_name = icon_name
        self.update()

    def set_notch(self, notch: bool) -> None:
        """Mark this as a control that opens a menu, with the stills bar's
        corner notch rather than a chevron: the chevron is a label's, and a
        bare glyph with one beside it is two controls' worth of width.
        """
        self._notch = notch
        self.update()

    def set_readout(self, readout: bool) -> None:
        """Show the glyph as a statement rather than a control: no hover, no
        notch, no click, and not greyed -- nothing is unavailable, there is
        just nothing to do.
        """
        self._readout = readout
        self.setCursor(
            Qt.CursorShape.ArrowCursor if readout else Qt.CursorShape.PointingHandCursor
        )
        self.update()

    def set_active(self, active: bool) -> None:
        self._active = active
        self.update()

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = enabled
        self.setCursor(
            Qt.CursorShape.PointingHandCursor if enabled else Qt.CursorShape.ArrowCursor
        )
        self.update()

    def enterEvent(self, event) -> None:
        self._hovered = True
        self.update()

    def leaveEvent(self, event) -> None:
        self._hovered = False
        self.update()

    def mousePressEvent(self, event) -> None:
        # Accepted even when disabled: this bar sits over a frozen overlay
        # that reads an unhandled press as the start of a drag, so letting
        # one through would start a selection behind the bar the user was
        # actually aiming at.
        event.accept()

    def mouseReleaseEvent(self, event) -> None:
        if (
            not self._enabled
            or self._readout
            or event.button() != Qt.MouseButton.LeftButton
        ):
            return
        # Pressing a control then sliding off it is how a user says "no".
        if self.rect().contains(event.position().toPoint()):
            self.clicked.emit()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        metric = tokens.FlowMetric

        if self._active or (self._hovered and self._enabled and not self._readout):
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(
                design.flow_color(
                    "TOOL_ACTIVE_BG" if self._active else "ROW_HOVER_BG"
                )
            )
            painter.drawRoundedRect(
                QRectF(self.rect()), metric.BTN_RADIUS, metric.BTN_RADIUS
            )

        if not self._enabled:
            token = "TOOL_DISABLED_FG"
        elif self._active:
            token = "TOOL_ACTIVE_FG"
        else:
            token = "TOOL_IDLE_FG"
        icon = design.icon(self._icon_name, design.flow_color(token))
        size = metric.ICON
        offset = (metric.BTN - size) / 2
        icon.paint(painter, round(offset), round(offset), size, size)
        if self._notch and not self._readout:
            _paint_notch(painter, self.width(), self.height(), enabled=self._enabled)
        painter.end()


class _LabelledIcon(_IconButton):
    """An icon button that also says what it is set to, with a chevron.

    Used where the answer matters more than the control: which destination
    a recording is headed for is the one thing on the ready bar a glyph
    alone would leave the user guessing at, so it is named.
    """

    def __init__(self, icon_name: str, label: str, parent: QWidget | None = None):
        self._label = label
        super().__init__(icon_name, parent)
        self._relayout()

    def label(self) -> str:
        return self._label

    def set_content(self, icon_name: str, label: str) -> None:
        self._icon_name = icon_name
        self._label = label
        self._relayout()

    def _relayout(self) -> None:
        metric = tokens.FlowMetric
        width = QFontMetricsF(_font(*tokens.BarFont.CHIP)).horizontalAdvance(self._label)
        self.setFixedWidth(
            math.ceil(metric.PAD + metric.ICON + 6 + width + 5 + metric.CHEVRON + metric.PAD)
        )
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        metric = tokens.FlowMetric
        if self._hovered and self._enabled:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(design.flow_color("ROW_HOVER_BG"))
            painter.drawRoundedRect(
                QRectF(self.rect()), metric.BTN_RADIUS, metric.BTN_RADIUS
            )

        tint = design.flow_color(
            "TOOL_DISABLED_FG" if not self._enabled else "TOOL_IDLE_FG"
        )
        x = float(metric.PAD)
        size = metric.ICON
        design.icon(self._icon_name, tint).paint(
            painter, round(x), (self.height() - size) // 2, size, size
        )
        x += size + 6

        painter.setFont(_font(*tokens.BarFont.CHIP))
        painter.setPen(tint)
        painter.drawText(
            QRectF(x, 0, self.width() - x - metric.CHEVRON - metric.PAD, self.height()),
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            self._label,
        )

        design.icon("chevron", tint).paint(
            painter,
            self.width() - metric.PAD - metric.CHEVRON,
            (self.height() - metric.CHEVRON) // 2,
            metric.CHEVRON,
            metric.CHEVRON,
        )
        painter.end()


class _IconTextButton(_IconButton):
    """An icon button with a text label beside it, for a control whose
    label names what the next click does (Pause, then "Resume · 0:12").
    """

    def __init__(self, icon_name: str, label: str, parent: QWidget | None = None):
        self._label = label
        super().__init__(icon_name, parent)
        self._relayout()

    def set_content(self, icon_name: str, label: str) -> None:
        self._icon_name = icon_name
        self._label = label
        self._relayout()

    def _relayout(self) -> None:
        metric = tokens.FlowMetric
        width = QFontMetricsF(_font(12, 500)).horizontalAdvance(self._label)
        self.setFixedWidth(round(metric.PAD + metric.ICON + 6 + width + metric.PAD))
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        metric = tokens.FlowMetric
        if self._hovered and self._enabled:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(design.flow_color("ROW_HOVER_BG"))
            painter.drawRoundedRect(
                QRectF(self.rect()), metric.BTN_RADIUS, metric.BTN_RADIUS
            )

        tint = design.flow_color(
            "TOOL_DISABLED_FG" if not self._enabled else "TOOL_IDLE_FG"
        )
        x = float(metric.PAD)
        icon = design.icon(self._icon_name, tint)
        size = metric.ICON
        icon.paint(painter, round(x), (self.height() - size) // 2, size, size)
        x += size + 6

        painter.setFont(_font(12, 500))
        painter.setPen(tint)
        painter.drawText(
            QRectF(x, 0, self.width() - x - metric.PAD, self.height()),
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            self._label,
        )
        painter.end()


class _DelayButton(_IconButton):
    """The timer, carrying its value once one is set -- the chooser's delay
    flag, on this bar.

    It used to be the bare glyph whatever was set, so a 10s countdown
    carried over from the chooser was invisible until Record was pressed and
    nothing happened for ten seconds. A countdown is the one thing here that
    can surprise you, so it earns width exactly when it is armed.
    """

    def __init__(self, parent: QWidget | None = None):
        super().__init__("timer", parent)
        self._value = ""
        self.set_value("")

    def value(self) -> str:
        return self._value

    def set_value(self, value: str) -> None:
        metric = tokens.BarMetric
        self._value = value
        width = metric.BTN
        if value:
            width = round(
                2 * metric.FLAG_PAD_H + tokens.FlowMetric.ICON + metric.FLAG_GAP
                + QFontMetricsF(_font(*tokens.BarFont.DELAY, mono=True)).horizontalAdvance(value)
            )
        self.setFixedWidth(width)
        self.update()

    def paintEvent(self, event) -> None:
        if not self._value:
            super().paintEvent(event)
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        metric = tokens.FlowMetric
        wash = design.flow_color("ACCENT_WASH")
        if self._hovered and self._enabled:
            wash = wash.lighter(125)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(wash)
        painter.drawRoundedRect(QRectF(self.rect()), metric.BTN_RADIUS, metric.BTN_RADIUS)

        foreground = design.flow_color("ACCENT_SOFT")
        x = tokens.BarMetric.FLAG_PAD_H
        size = metric.ICON
        design.icon(self._icon_name, foreground).paint(
            painter, x, round((self.height() - size) / 2), size, size
        )
        x += size + tokens.BarMetric.FLAG_GAP
        painter.setFont(_font(*tokens.BarFont.DELAY, mono=True))
        painter.setPen(foreground)
        painter.drawText(
            QRectF(x, 0, self.width() - x, self.height()),
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            self._value,
        )
        if self._notch:
            _paint_notch(painter, self.width(), self.height(), enabled=self._enabled)
        painter.end()


class _TextButton(_IconButton):
    """A bare word. Cancel, where a cross would read as "close the bar"
    rather than "do not record this".
    """

    def __init__(self, label: str, parent: QWidget | None = None):
        self._label = label
        super().__init__("close", parent)
        width = QFontMetricsF(_font(12, 500)).horizontalAdvance(label)
        self.setFixedWidth(round(width + tokens.FlowMetric.PAD * 2 + 8))

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        metric = tokens.FlowMetric
        if self._hovered and self._enabled:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(design.flow_color("ROW_HOVER_BG"))
            painter.drawRoundedRect(
                QRectF(self.rect()), metric.BTN_RADIUS, metric.BTN_RADIUS
            )
        painter.setFont(_font(12, 500))
        painter.setPen(design.flow_color("TOOL_IDLE_FG"))
        painter.drawText(
            self.rect(), int(Qt.AlignmentFlag.AlignCenter), self._label
        )
        painter.end()


class _ActionButton(QWidget):
    """The bar's one accent-filled control, at the left end (rule 3).

    Built to the stills bar's split button's face (`overlay._SplitAction`)
    -- the same padding, glyph box and label face -- so the two bars'
    primary actions read as one control, which they did not while this one
    kept the flow handoff's larger face and an inline `↵`.
    """

    clicked = pyqtSignal()

    def __init__(
        self,
        label: str,
        *,
        glyph: str | None = None,
        shortcut: str = "",
        tone: str = "accent",
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self._label = label
        self._glyph = glyph
        self._shortcut = shortcut
        self._tone = tone
        self._hovered = False
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(tokens.FlowMetric.BTN)
        self._relayout()

    def set_label(
        self, label: str, *, shortcut: str | None = None, glyph: str | None = "keep"
    ) -> None:
        self._label = label
        if shortcut is not None:
            self._shortcut = shortcut
        if glyph != "keep":
            self._glyph = glyph
        self._relayout()

    def set_tone(self, tone: str) -> None:
        self._tone = tone
        self.update()

    def _relayout(self) -> None:
        metric = tokens.BarMetric
        glyph_w = (metric.SPLIT_ICON + metric.SPLIT_GAP) if self._glyph else 0
        # Rounded up: a face a fraction of a pixel narrow clips the label's
        # last glyph.
        self.setFixedWidth(math.ceil(metric.SPLIT_PAD_H * 2 + glyph_w + self._text_width()))
        self.updateGeometry()
        self.update()

    def _text_width(self) -> float:
        width = QFontMetricsF(_font(*tokens.BarFont.SPLIT)).horizontalAdvance(self._label)
        if self._shortcut:
            width += QFontMetricsF(_font(11, 500, mono=True)).horizontalAdvance(
                f"  {self._shortcut}"
            )
        return width

    def sizeHint(self) -> QSize:
        return QSize(self.width(), tokens.FlowMetric.BTN)

    def enterEvent(self, event) -> None:
        self._hovered = True
        self.update()

    def leaveEvent(self, event) -> None:
        self._hovered = False
        self.update()

    def mousePressEvent(self, event) -> None:
        event.accept()

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self.rect().contains(
            event.position().toPoint()
        ):
            self.clicked.emit()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        metric = tokens.BarMetric

        fill = design.flow_color("REC" if self._tone == "rec" else "ACCENT")
        if self._hovered:
            fill = fill.lighter(108)
        text = design.flow_color("REC_FG" if self._tone == "rec" else "ACCENT_FG")

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(fill)
        painter.drawRoundedRect(QRectF(self.rect()), metric.BTN_RADIUS, metric.BTN_RADIUS)

        x = float(metric.SPLIT_PAD_H)
        if self._glyph:
            # Record is a filled circle rather than an icon -- the handoff is
            # specific about it, because every icon set's "record" is a
            # circle anyway and a stroked one reads as a radio button. Stop
            # is the matching square: a circle beside the word "Stop" reads
            # as record whatever the label says, which is the shape of
            # mistake this whole redesign exists to stop making. Centred in
            # the glyph box the stills split's icon sits in.
            diameter = 10
            left = x + (metric.SPLIT_ICON - diameter) / 2
            top = (self.height() - diameter) / 2
            painter.setBrush(text)
            if self._glyph == "square":
                painter.drawRect(QRectF(left, top, diameter, diameter))
            else:
                painter.drawEllipse(QRectF(left, top, diameter, diameter))
            x += metric.SPLIT_ICON + metric.SPLIT_GAP

        painter.setPen(text)
        painter.setFont(_font(*tokens.BarFont.SPLIT))
        rect = QRectF(x, 0, self.width() - x - metric.SPLIT_PAD_H, self.height())
        painter.drawText(
            rect, int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            self._label,
        )

        if self._shortcut:
            painter.setFont(_font(11, 500, mono=True))
            painter.drawText(
                rect,
                int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
                self._shortcut,
            )
        painter.end()


class _Readout(QLabel):
    """A mono readout: the clock, the size, the summary chip."""

    def __init__(self, parent: QWidget | None = None, *, token: str = "ROW_IDLE_FG"):
        super().__init__(parent)
        self._token = token
        self._wash: str | None = None
        self.setFont(_font(12.5, 600, mono=True))
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._apply()

    def set_token(self, token: str) -> None:
        self._token = token
        self._apply()

    def set_wash(self, token: str | None) -> None:
        """A tinted plate behind the text, or None for bare text.

        The live clock reads on a red wash because red is what says "live"
        in this design; the same readout with no wash is the finished
        recording's summary, which is over and should not.
        """
        self._wash = token
        self._apply()

    def _apply(self) -> None:
        rules = [f"color: {design.flow_color(self._token).name()};"]
        if self._wash:
            wash = design.flow_color(self._wash)
            rules.append(
                f"background: rgba({wash.red()}, {wash.green()}, "
                f"{wash.blue()}, {wash.alphaF():.2f});"
            )
            rules.append(f"border-radius: {tokens.FlowMetric.BTN_RADIUS}px;")
            rules.append("padding: 0px 8px;")
        self.setStyleSheet(" ".join(rules))


class MenuReopenGuard:
    """Why a menu does not reopen when the chip that opened it is clicked
    again.

    A popup takes the mouse for as long as it is up, so the press that
    dismisses it never reaches the chip underneath -- until the popup has
    closed, at which point that same press arrives at the chip, which opens
    the menu it just closed. What the user sees is a menu that cannot be
    shut by clicking the control that opened it: it flickers and comes
    straight back. `WA_NoMouseReplay` is Qt's answer and does not cover
    this, because the chip is a widget in another top-level window rather
    than under the popup's own replay.

    Elapsed time alone cannot tell that press apart from a deliberate
    second click, so this also asks *where the pointer is*: only the chip
    the pointer is actually over is blocked, which is what lets clicking a
    different chip close one menu and open the other in a single press --
    the behaviour the bars are supposed to have.

    One guard per opener. Shared by the chooser row and every bar menu,
    which all have the same chip-opens-a-popup shape.
    """

    # Long enough to cover the popup closing and the press being delivered,
    # short enough that a person clicking the same chip twice on purpose is
    # never refused: a deliberate reopen is hundreds of milliseconds away.
    WINDOW_MS = 250

    def __init__(self) -> None:
        self._closed_at: float | None = None

    def note_closed(self) -> None:
        """Called when the menu closes, however it closed."""
        self._closed_at = time.monotonic()

    def blocks_reopen(self, opener: QWidget) -> bool:
        """Whether an open request from `opener` is that dismissing press
        arriving late, rather than a new click."""
        if self._closed_at is None:
            return False
        if (time.monotonic() - self._closed_at) * 1000 > self.WINDOW_MS:
            return False
        origin = opener.mapToGlobal(QPoint(0, 0))
        return QRect(origin, opener.size()).contains(QCursor.pos())


class FlowMenu(QWidget):
    """A dropdown for one of the bars.

    A **top-level popup**, never a child of the bar that opened it. The
    handoff is explicit about this and gives the reason: an open menu has to
    paint above the hint pill that sits *below* the bar, and a parent
    carrying an effect traps it in that parent's stacking context. The HTML
    reference hit exactly this with `backdrop-filter`.

    Rows are `(value, label, note, shortcut, disabled_reason)`. A row with a
    reason is drawn dimmed and refuses to be chosen, rather than being left
    out: the handoff's rule is that an option which cannot work says why,
    because a user who cannot see the reason has no way to tell a missing
    feature from a broken one.
    """

    chosen = pyqtSignal(str)

    def hideEvent(self, event) -> None:  # noqa: N802 - Qt override
        """Every way this closes -- a pick, a press outside, Escape -- ends
        in a hide, so this is the one place the guard has to be told."""
        if self._guard is not None:
            self._guard.note_closed()
        super().hideEvent(event)

    def __init__(
        self,
        rows,
        current: str,
        width: int,
        parent: QWidget | None = None,
        footnote: str = "",
        *,
        guard: "MenuReopenGuard | None" = None,
    ):
        super().__init__(parent)
        # Told here rather than wired by every caller: a menu knows when it
        # closes, and the chip that opened it has to know too or it reopens
        # on the press that dismissed this. See MenuReopenGuard.
        self._guard = guard
        self.setWindowFlags(
            Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        # A top-level window with no parent has no host to find: a caller
        # opening this over the frozen frame names it (`Glass.set_host`).
        # Anywhere else the menu's own 98% already clears the fallback's.
        self.glass = glass.Glass(self)
        self._rows = list(rows)
        self._current = current
        self._footnote = footnote
        self._hovered = -1
        self.setMouseTracking(True)

        metric = tokens.FlowMetric
        row_h = self._row_height()
        height = metric.MENU_PAD * 2 + row_h * len(self._rows)
        self.setFixedSize(width, height + self._footnote_height(width))

    def _footnote_height(self, width: int) -> int:
        """Room for the standing note under the rows, separator included.

        Zero when there is no footnote, so every menu that does not want one
        is the size it always was.
        """
        if not self._footnote:
            return 0
        return 11 + round(self._footnote_rect(width).height())

    def _footnote_rect(self, width: int) -> QRectF:
        metric = tokens.FlowMetric
        _pad_v, pad_h = metric.MENU_ROW_PAD
        available = width - (metric.MENU_PAD + pad_h) * 2
        metrics = QFontMetrics(_font(11, 400))
        bounds = metrics.boundingRect(
            QRect(0, 0, available, 0),
            int(Qt.TextFlag.TextWordWrap),
            self._footnote,
        )
        return QRectF(0, 0, available, bounds.height() + 6)

    @staticmethod
    def _row_height() -> int:
        metric = tokens.FlowMetric
        pad_v, _pad_h = metric.MENU_ROW_PAD
        return pad_v * 2 + 30

    def _row_at(self, y: float) -> int:
        metric = tokens.FlowMetric
        index = int((y - metric.MENU_PAD) // self._row_height())
        return index if 0 <= index < len(self._rows) else -1

    def _usable_area(self, anchor_rect):
        """The available area of the screen `anchor_rect` sits on, or None
        when there is no screen to ask (the offscreen platform).

        `anchor_rect` is in absolute logical virtual-desktop coordinates --
        every caller maps its control through `mapToGlobal` first -- which
        is the same space `availableGeometry()` answers in, so the two are
        comparable without conversion.
        """
        screen = QGuiApplication.screenAt(anchor_rect.center())
        return screen.availableGeometry() if screen is not None else None

    def _open_at(self, anchor_rect, *, above: bool, within=None) -> None:
        """Place this menu on the wanted side of `anchor_rect`, flip to the
        other side when it would not fit, and keep it inside `within`
        whatever happens.

        Both the flip and the clamp are why this exists. The delay menu
        opened downward unconditionally, so with the recording bar low on
        the screen its rows ran off the bottom edge and under the taskbar,
        where "5s" could not be clicked at all. And nothing ever looked at
        the left and right edges, so a menu anchored to a control near a
        screen edge ran off sideways for the same reason.

        Every rect here is absolute logical virtual-desktop coordinates.
        """
        metric = tokens.FlowMetric
        if within is None:
            within = self._usable_area(anchor_rect)

        top_if_above = anchor_rect.top() - metric.MENU_OFFSET - self.height()
        top_if_below = anchor_rect.bottom() + metric.MENU_OFFSET
        y = top_if_above if above else top_if_below
        if within is not None:
            fits_above = top_if_above >= within.top()
            fits_below = top_if_below + self.height() <= within.bottom()
            if above and not fits_above and fits_below:
                y = top_if_below
            elif not above and not fits_below and fits_above:
                y = top_if_above

        x = anchor_rect.center().x() - self.width() / 2
        if within is not None:
            # Clamped last, and to the left edge if the menu is somehow
            # wider than the screen: a menu whose left edge is off-screen
            # has lost its first column of text, which is the one carrying
            # the labels.
            x = min(x, within.right() - self.width())
            x = max(x, within.left())
            y = min(y, within.bottom() - self.height())
            y = max(y, within.top())
        self.move(round(x), round(y))
        self.show()

    def open_above(self, anchor_rect, within=None) -> None:
        """Open with the menu's bottom edge above `anchor_rect`'s top.

        The audio menu opens upward so it never covers the region being
        recorded -- the one thing on screen the user is trying to look at.
        Flips below rather than leaving the screen when there is no room.
        """
        self._open_at(anchor_rect, above=True, within=within)

    def open_below(self, anchor_rect, within=None) -> None:
        """Open under `anchor_rect`, flipping above when there is no room
        below -- which is what a bar low on the screen leaves."""
        self._open_at(anchor_rect, above=False, within=within)

    def open_clear_of(self, anchor_rect, within=None) -> None:
        """Open above `anchor_rect` when it fits inside `within` -- the
        anchor's screen's available area when not given -- and below when
        it does not.

        The recording bar sits top-centre, 12px under the top of the
        usable area, precisely because the region is usually below it. A
        menu that only ever opened upward went off the top of the screen
        there, where nothing could reach it.
        """
        self.open_above(anchor_rect, within)

    @staticmethod
    def fitting_width(rows, minimum: int) -> int:
        """`minimum`, or wider if a row's label or note would not fit in it.

        Measured in the fonts actually in use rather than trusted to a
        token tuned to Plex (flow/divergences.md 8): a note that overruns
        is clipped mid-word, not wrapped.
        """
        metric = tokens.FlowMetric
        _pad_v, pad_h = metric.MENU_ROW_PAD
        label = QFontMetricsF(_font(12.5, 600))
        note = QFontMetricsF(_font(11, 400))
        key = QFontMetricsF(_font(11, 500, mono=True))
        widest = 0.0
        for _value, text, sub, shortcut, disabled in rows:
            line = label.horizontalAdvance(text)
            if shortcut:
                line += 12 + key.horizontalAdvance(shortcut)
            widest = max(widest, line, note.horizontalAdvance(disabled or sub))
        return max(minimum, math.ceil(widest + 2 * (metric.MENU_PAD + pad_h)))

    def mouseMoveEvent(self, event) -> None:
        index = self._row_at(event.position().y())
        if index != self._hovered:
            self._hovered = index
            self.update()

    def leaveEvent(self, event) -> None:
        self._hovered = -1
        self.update()

    def mouseReleaseEvent(self, event) -> None:
        index = self._row_at(event.position().y())
        if index < 0:
            return
        value, _label, _note, _shortcut, disabled = self._rows[index]
        if disabled:
            return
        self.chosen.emit(value)
        self.close()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        metric = tokens.FlowMetric

        surface = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        path = glass.rounded(surface, metric.MENU_RADIUS)
        self.glass.paint(painter, path, design.flow_color("MENU_BG"))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(design.flow_color("MENU_BORDER"))
        painter.drawPath(path)

        row_h = self._row_height()
        pad_v, pad_h = metric.MENU_ROW_PAD
        for index, (value, label, note, shortcut, disabled) in enumerate(self._rows):
            top = metric.MENU_PAD + index * row_h
            row = QRectF(metric.MENU_PAD, top, self.width() - metric.MENU_PAD * 2, row_h)
            selected = value == self._current

            if selected or (index == self._hovered and not disabled):
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(
                    design.flow_color(
                        "ROW_SELECTED_BG" if selected else "ROW_HOVER_BG"
                    )
                )
                painter.drawRoundedRect(
                    row, metric.MENU_ROW_RADIUS, metric.MENU_ROW_RADIUS
                )

            if disabled:
                fg = design.flow_color("TOOL_DISABLED_FG")
            elif selected:
                fg = design.flow_color("ROW_SELECTED_FG")
            else:
                fg = design.flow_color("ROW_IDLE_FG")

            text_rect = row.adjusted(pad_h, 0, -pad_h, 0)
            painter.setPen(fg)
            painter.setFont(_font(12.5, 600 if selected else 500))
            if note or disabled:
                painter.drawText(
                    text_rect.adjusted(0, pad_v - 2, 0, 0),
                    int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop),
                    label,
                )
                painter.setPen(design.flow_color("ROW_NOTE_FG"))
                painter.setFont(_font(11, 400))
                painter.drawText(
                    text_rect.adjusted(0, 0, 0, -pad_v + 2),
                    int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom),
                    disabled or note,
                )
            else:
                painter.drawText(
                    text_rect,
                    int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                    label,
                )

            if shortcut and not disabled:
                painter.setPen(design.flow_color("SHORTCUT_FG"))
                painter.setFont(_font(11, 500, mono=True))
                painter.drawText(
                    text_rect,
                    int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
                    shortcut,
                )

        if self._footnote:
            # Permanently at the foot of the menu, not attached to any row:
            # it is true of every export, and repeating it per row would
            # read as four different warnings.
            top = metric.MENU_PAD + row_h * len(self._rows)
            painter.setPen(design.flow_color("MENU_BORDER"))
            painter.drawLine(
                QPointF(metric.MENU_PAD + 4, top + 5),
                QPointF(self.width() - metric.MENU_PAD - 4, top + 5),
            )
            note = self._footnote_rect(self.width())
            note.moveTo(metric.MENU_PAD + pad_h, top + 11)
            painter.setPen(design.flow_color("ROW_NOTE_FG"))
            painter.setFont(_font(11, 400))
            painter.drawText(
                note,
                int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop
                    | Qt.TextFlag.TextWordWrap),
                self._footnote,
            )
        painter.end()


class RecordingBar(QWidget):
    """The recording side of the flow, stages 3b through 6.

    Four states on one widget -- `ready`, `counting`, `live`, `done` -- and
    every control for all four is built in `__init__` and then shown or
    hidden. Rebuilding the layout per state would change the bar's width and
    therefore move both its edges, which rule 1 forbids; it is also what
    makes a control's identity survive a state change, so a click already in
    flight lands on the thing the user pressed.

    One control the handoff specifies is deliberately absent by default,
    per docs/design/flow/divergences.md 5: the `done` state's destination
    button. The destination is chosen before recording, so the file lands on
    Stop; `set_done(destination=...)` puts the button back for a caller that
    wires it.

    **Pause** (#87) is built: a control on the `live` state, beside
    Stop, that always names what a click does next -- "Pause", then
    "Resume · 0:12" once paused. Whether the active backend can honour it
    at all is `RecordingBackend.can_pause`, a capability the backend
    declares rather than this widget guessing; where it can't, the control
    stays visible and greyed with its reason
    (`set_pause_enabled`/`pause_control`), the same "an option that cannot
    work says why" rule `_audio` already follows for GNOME's missing audio
    route.
    """

    startClicked = pyqtSignal()
    # The ready stage's destination chip: offer the other endings.
    destinationMenuRequested = pyqtSignal()
    cancelClicked = pyqtSignal()
    stopClicked = pyqtSignal()
    pauseClicked = pyqtSignal()
    audioClicked = pyqtSignal()
    delayClicked = pyqtSignal()
    destinationClicked = pyqtSignal()
    discardClicked = pyqtSignal()

    READY = "ready"
    COUNTING = "counting"
    LIVE = "live"
    DONE = "done"

    def __init__(self, parent: QWidget | None = None):
        # Parentless and always-on-top by default: this is a HUD standing in
        # for a window, the same shape `DelayCountdown` and the old
        # the pill this replaces used.
        super().__init__(parent)
        metric = tokens.FlowMetric
        # `Tool`, not a plain top-level. Without it the window manager
        # treats this as an ordinary window and will not stage it above the
        # overlay's fullscreen one -- the bar is created, placed and shown,
        # and never seen. Measured: two bare windows with these flags do
        # stack correctly and two without the Tool flag do not, which is
        # why `RegionFrame`'s strips were visible all along and this was
        # not. It also keeps the bar out of the task switcher, which is
        # right for a HUD.
        #
        # A window of its own only without a parent. Where the compositor
        # will not let an app place its own window (Wayland), `app.py` puts
        # the ready bar inside the overlay instead, as the stills bar is.
        if parent is None:
            self.setWindowFlags(
                Qt.WindowType.FramelessWindowHint
                | Qt.WindowType.WindowStaysOnTopHint
                | Qt.WindowType.Tool
            )
        # Shown without stealing focus: the overlay underneath owns the
        # keyboard while a recording is armed (Enter starts it, Esc
        # cancels), and a bar that took focus would break both.
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        # A top-level window has no parent to find the frozen frame through,
        # so the caller names the overlay while one is up behind this bar
        # (`set_backdrop_host`).
        self.glass = glass.Glass(self)
        self._state = self.READY
        # All three are read by `set_ready()` below, so they exist before it
        # runs.
        self._delay_available = True
        self._last_shown: dict[str, bool] = {}
        self._destination = tokens.RECORD_AFTER_DEFAULT

        # ROW_H is "6 pad + 28 control + 6 pad + 2x1px border", so the
        # vertical margin carries the border's pixel as well as the pad --
        # otherwise the bar comes out 40 tall against a token that says 42,
        # and every placement measured from it is two pixels wrong.
        border = (metric.ROW_H - metric.BTN - 2 * metric.PAD) // 2
        layout = QHBoxLayout(self)
        layout.setContentsMargins(
            metric.PAD, metric.PAD + border, metric.PAD, metric.PAD + border
        )
        layout.setSpacing(metric.GAP)
        self.setFixedHeight(metric.ROW_H)

        # The clock is added first and so sits to the LEFT of the action,
        # which is the handoff's own layout for the live stage ("clock ->
        # Stop") and the one place the bar departs from rule 3. It is safe
        # because the clock is mono: 0:09 -> 0:10 is the same width, so
        # Stop does not shuffle every second. It moves once, at 9:59.
        self._clock = _Readout(self, token="REC_CLOCK")
        layout.addWidget(self._clock)

        self._action = _ActionButton("Record", glyph="circle", parent=self)
        self._action.clicked.connect(self._on_action)
        layout.addWidget(self._action)

        # Beside Stop, live state only -- see the class docstring's Pause
        # note.
        self._pause = _IconTextButton("pause", "Pause", self)
        self._pause.clicked.connect(self.pauseClicked)
        layout.addWidget(self._pause)

        self._action_divider = _Divider(self)
        layout.addSpacing(metric.GROUP_GAP - metric.GAP)
        layout.addWidget(self._action_divider)
        layout.addSpacing(metric.GROUP_GAP - metric.GAP)

        # What Stop will do with the recording, named: the one decision on
        # this bar a glyph alone would leave the user guessing at. Its own
        # control rather than a caret on Record, so pressing Record never
        # risks landing on the menu instead.
        self._destination_chip = _LabelledIcon("copy", "Copy", self)
        self._destination_chip.clicked.connect(self.destinationMenuRequested)
        layout.addWidget(self._destination_chip)

        # A glyph and a notch, not the spec's labelled "System ⌄" chip. The
        # three sources have three glyphs -- speaker, mic, struck speaker --
        # so the glyph does answer "which", and the tooltip names it. The
        # label cost the bar its widest slot for a decision made now and
        # then, and on Linux, which has no audio route at all, it spent it
        # on a greyed word that could never change.
        self._audio = _IconButton("mute", self)
        self._audio.set_notch(True)
        self._audio.clicked.connect(self.audioClicked)
        layout.addWidget(self._audio)

        self._delay = _DelayButton(self)
        self._delay.set_notch(True)
        self._delay.clicked.connect(self.delayClicked)
        layout.addWidget(self._delay)

        self._summary = _Readout(self, token="ROW_NOTE_FG")
        layout.addWidget(self._summary)

        self._tail_divider = _Divider(self)
        layout.addSpacing(metric.GROUP_GAP - metric.GAP)
        layout.addWidget(self._tail_divider)
        layout.addSpacing(metric.GROUP_GAP - metric.GAP)

        # A word, not a cross. The spec spells Cancel out, and at this
        # stage nothing has happened yet -- an X beside a Record button
        # reads as "close the bar", which is not what it does.
        self._cancel = _TextButton("Cancel", self)
        self._cancel.clicked.connect(self.cancelClicked)
        layout.addWidget(self._cancel)

        self._discard = _IconButton("trash", self)
        self._discard.clicked.connect(self.discardClicked)
        layout.addWidget(self._discard)

        self.set_ready()

    # -- state ---------------------------------------------------------
    def state(self) -> str:
        return self._state

    def set_ready(self) -> None:
        """Stage 3b. The one stage where the selection can still be resized,
        which is why the hint says so and why Record is the only accent.
        """
        self._state = self.READY
        self._action.set_label("Record", shortcut="", glyph="circle")
        self._audio.set_readout(False)
        self._action.setToolTip("Start recording — Enter")
        self._action.set_tone("accent")
        self._show(action=True, clock=False, pause=False, destination=True, audio=True,
                   delay=self._delay_available,
                   summary=False, cancel=True, discard=False)

    def set_counting(self, seconds: int) -> None:
        """Stage 4. The numeral itself goes inside the region -- see
        `CountdownNumeral` -- so the bar reduces to an armed pill and a way
        out, and nothing here restates the count.
        """
        self._state = self.COUNTING
        self._action.set_label(f"Starting in {seconds}", shortcut="", glyph="circle")
        self._action.setToolTip("")
        self._action.set_tone("accent")
        self._show(action=True, clock=False, pause=False, audio=False, delay=False,
                   summary=False, cancel=True, discard=False)

    def clock_text(self) -> str:
        """What the clock slot currently reads.

        It does not always hold a clock -- it carries "Starting" between the
        Start press and the recorder actually running -- so the accessor is
        named for the slot rather than for the number usually in it.
        """
        return self._clock.text()

    def set_live(self, elapsed: str, *, size: str = "") -> None:
        """Stage 5. The clock leads, then Stop.

        The clock leads, which is the handoff's own layout here and the one
        place the bar departs from rule 3's "action at the left end". The
        departure costs nothing because the clock is mono -- 0:09 and 0:10
        are the same width, so Stop does not shuffle sideways every second;
        it moves once, when the recording passes ten minutes.
        """
        self._state = self.LIVE
        self._clock.setText(elapsed)
        self._clock.set_wash("REC_WASH")
        self._summary.setText(size)
        self._action.set_label("Stop", shortcut="", glyph="square")
        self._action.setToolTip("")
        self._action.set_tone("rec")
        # Whether this call is a fresh start or a resume from `set_paused`,
        # "recording" always means the Pause control offers to pause --
        # resuming through `set_live` rather than a dedicated method is
        # what makes several pause/resume cycles just work.
        self._pause.set_content("pause", "Pause")
        # What is being recorded, not a choice: the source is read when the
        # recorder starts, so a menu here would change nothing it records.
        self._audio.set_readout(True)
        self._show(action=True, clock=True, pause=True, audio=True, delay=False,
                   summary=bool(size), cancel=False, discard=False)

    def set_paused(self, elapsed: str, paused_for: str) -> None:
        """The `live` state's paused sub-state: the clock freezes at
        `elapsed` -- the recorded length so far, paused time already
        excluded -- and the Pause control becomes Resume, growing by
        `paused_for` so the user can see how long the pause itself has
        run. Still `LIVE`: Stop and Discard work exactly as they do while
        recording, per the class docstring.
        """
        self._clock.setText(elapsed)
        self._pause.set_content("play", f"Resume · {paused_for}")

    def set_done(self, summary: str, *, destination: str | None = None) -> None:
        """Stage 6: what was produced, and a way to decide it was not worth
        keeping.

        `destination is None` -- the shipped shape -- shows the summary and
        Discard alone, because the file has already landed and there is
        nothing left to confirm. Passing one puts it back as an accent
        action, which is the handoff's own stage 6; do not pass one without
        wiring `destinationClicked`, or the bar grows exactly the
        visibly-enabled control doing nothing that this design exists to
        remove.
        """
        self._state = self.DONE
        if destination is not None:
            # No glyph: the destination is a word, and a record dot beside
            # "Copy" would say the recording is still running.
            self._action.set_label(destination, shortcut="↵", glyph=None)
            self._action.set_tone("accent")
        self._summary.setText(summary)
        self._clock.set_wash(None)
        self._show(action=destination is not None, clock=False, pause=False, audio=False,
                   delay=False, summary=True, cancel=False, discard=True)

    def set_backdrop_host(self, host: QWidget | None) -> None:
        """Be glass over `host`'s frozen frame -- the overlay, while the
        ready stage keeps it up -- or, with None, over nothing this process
        painted: the live desktop, once the overlay has gone.
        """
        self.glass.set_host(host)
        self.update()

    # -- destination -------------------------------------------------
    def set_destination(self, destination: str) -> None:
        """Name what Stop will do on the destination chip -- one of
        `tokens.RECORD_DESTINATIONS`' ids, or the default for anything
        else. The bar only says it; the caller owns what the menu offers.
        """
        rows = {
            identifier: (glyph, label)
            for identifier, glyph, label, _note in tokens.RECORD_DESTINATIONS
        }
        if destination not in rows:
            destination = tokens.RECORD_AFTER_DEFAULT
        self._destination = destination
        self._destination_chip.set_content(*rows[destination])
        self.adjustSize()

    def destination(self) -> str:
        return self._destination

    def destination_control(self) -> QWidget:
        """The destination chip, for its menu to anchor against."""
        return self._destination_chip

    # -- audio ---------------------------------------------------------
    def set_audio(self, source: str) -> None:
        """Reflect the chosen source. The bar renders it; whether a source is
        even offerable is the platform's business, not this widget's.
        """
        icon, label = {
            identifier: (icon, label)
            for identifier, icon, label, _note in tokens.AUDIO_SOURCES
        }.get(source, ("mute", "Muted"))
        self._audio.set_icon(icon)
        self._audio.setToolTip(f"Audio: {label}")

    def set_audio_enabled(self, enabled: bool) -> None:
        self._audio.set_enabled(enabled)

    def delay_control(self) -> QWidget:
        """The delay button, for a menu to anchor itself against."""
        return self._delay

    def set_delay(self, delay: str) -> None:
        """Show the countdown Record will start with -- one of
        `tokens.DELAYS`, the first of which is none and shows nothing.
        """
        self._delay.set_value("" if delay == tokens.DELAYS[0] else delay)
        self.adjustSize()

    def audio_control(self) -> QWidget:
        """The audio button itself, so a caller can hang the platform's own
        reason on it as a tooltip. Handed out rather than taking the string
        here because *why* a platform cannot record audio is the platform's
        sentence to write, not this widget's.
        """
        return self._audio

    def set_delay_available(self, available: bool) -> None:
        """Show or hide the delay control.

        Hidden while it has no menu to open. The handoff puts a delay
        dropdown in this bar precisely because the stage without one had "a
        visibly-enabled control doing nothing", so shipping an inert one
        here would reintroduce that bug under a new name.
        """
        self._delay_available = available
        self._refresh_visibility()

    # -- pause -----------------------------------------------------------
    def set_pause_enabled(self, enabled: bool) -> None:
        """Grey the Pause control, or restore it -- never hide it.

        Whether a click can do anything is `RecordingBackend.can_pause`,
        answered fresh for the backend that actually started this
        recording, the same "the bar renders it, the caller decides it"
        split `set_audio_enabled` already draws for GNOME's missing audio
        route.
        """
        self._pause.set_enabled(enabled)

    def pause_control(self) -> QWidget:
        """The Pause/Resume button itself, so a caller can hang the
        backend's own `pause_unavailable_reason()` on it as a tooltip --
        the same handoff-out `audio_control()` already does for why
        audio is unavailable.
        """
        return self._pause

    # -- internals -----------------------------------------------------
    def _on_action(self) -> None:
        if self._state == self.READY:
            self.startClicked.emit()
        elif self._state == self.LIVE:
            self.stopClicked.emit()
        elif self._state == self.DONE:
            self.destinationClicked.emit()
        # COUNTING's action is the armed pill: it reports nothing, because
        # the only thing to do during a countdown is cancel, and Cancel is
        # its own control rather than a second meaning for this one.

    def _refresh_visibility(self) -> None:
        """Re-apply the current state's visibility, so a change in what is
        *available* takes effect without the caller re-entering the state.
        """
        if self._last_shown:
            shown = dict(self._last_shown)
            if self._state == self.READY:
                shown["delay"] = self._delay_available
            self._show(**shown)

    def _show(self, **visible: bool) -> None:
        self._last_shown = dict(visible)
        widgets = {
            "action": self._action,
            "clock": self._clock,
            "pause": self._pause,
            "destination": self._destination_chip,
            "audio": self._audio,
            "delay": self._delay,
            "summary": self._summary,
            "cancel": self._cancel,
            "discard": self._discard,
        }
        for name, widget in widgets.items():
            widget.setVisible(visible.get(name, False))

        # A divider earns its place only when there is something on both
        # sides of it; two dividers with nothing between them is how a bar
        # ends up looking broken in one state and fine in every other.
        head = (
            visible.get("action", False)
            or visible.get("clock", False)
            or visible.get("pause", False)
        )
        middle = (
            visible.get("destination", False)
            or visible.get("audio", False)
            or visible.get("delay", False)
            or visible.get("summary", False)
        )
        tail = visible.get("cancel", False) or visible.get("discard", False)
        # With the middle empty the two would meet -- the countdown's
        # "Starting in 3 | | Cancel" -- so the head's gives way to the tail's.
        self._action_divider.setVisible(head and middle)
        self._tail_divider.setVisible(tail and (head or middle))

        self.adjustSize()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        _glass(
            painter, self.glass, rect, tokens.FlowMetric.RADIUS,
            live=self._state == self.LIVE,
        )
        painter.end()


class CountdownNumeral(QWidget):
    """The pre-recording count, centred IN the region.

    Inside rather than on the bar because that is where the user is already
    looking -- they are watching the thing about to be filmed, not the
    chrome. The old build put the count on the pill and the first seconds of
    every recording were still of somebody looking away from the frame.

    Parentless for the same reason `DelayCountdown` is: it has to outlive
    whatever chrome is being taken down around it.
    """

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        diameter = tokens.FlowMetric.COUNT_D
        # `Tool` for the same stacking reason as `RecordingBar` -- and this
        # one has to clear the overlay, since it is shown over the region
        # while the overlay is still up.
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setFixedSize(diameter, diameter)
        self._seconds = 0

    def set_seconds(self, seconds: int) -> None:
        self._seconds = seconds
        self.update()

    def show_centered_on(self, rect) -> None:
        centre = QRectF(rect).center()
        half = tokens.FlowMetric.COUNT_D / 2
        self.move(round(centre.x() - half), round(centre.y() - half))
        self.show()
        self.raise_()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(design.flow_color("BAR_BG"))
        painter.drawEllipse(QRectF(self.rect()))

        pen = QPen(design.flow_color("REC"))
        pen.setWidth(2)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawEllipse(QRectF(self.rect()).adjusted(1, 1, -1, -1))

        painter.setPen(design.flow_color("ROW_SELECTED_FG"))
        painter.setFont(_font(tokens.FlowMetric.COUNT_FONT, 600, mono=True))
        painter.drawText(
            self.rect(), int(Qt.AlignmentFlag.AlignCenter), str(self._seconds)
        )
        painter.end()


class _Panel(QWidget):
    """One flat rectangle of colour, painted rather than styled.

    A stylesheet background is not reliable here: a plain `QWidget` does not
    paint one without `WA_StyledBackground`, and combined with
    `WA_TranslucentBackground` the widget is simply cleared to nothing --
    which is exactly how four correctly-sized, correctly-coloured, visible
    scrim panels managed to render as no scrim at all. Painting it is one
    line and cannot be undone by an attribute.
    """

    def __init__(self, colour: QColor):
        super().__init__(None)
        self._revealed_colour = QColor(colour)
        self._colour = QColor(colour)
        self._colour.setAlpha(0)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowTransparentForInput
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        # Recording-frame surfaces are mapped transparent and revealed only
        # after a compositor-specific platform has placed every edge.  This
        # prevents Wayland from showing four tool windows opening one by one
        # at their compositor-chosen staging positions.
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

    def reveal(self) -> None:
        self._colour = QColor(self._revealed_colour)
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), self._colour)
        painter.end()


class _OutlinePanel(_Panel):
    """One transparent surface whose paint lives outside the capture rect."""

    def __init__(self, colour: QColor, thickness: int):
        super().__init__(colour)
        self._thickness = thickness

    def resizeEvent(self, event) -> None:
        """Make the centre absent, not merely transparent.

        Hyprland may occlude the client beneath an alpha-zero part of a
        regular Wayland surface.  A native window mask gives the compositor
        only the four border strips, so the recorded region is not a window
        at all and cannot hide anything beneath it.
        """
        t = self._thickness
        width, height = self.width(), self.height()
        border = QRegion(0, 0, width, t)
        border = border.united(QRegion(0, height - t, width, t))
        border = border.united(QRegion(0, t, t, max(0, height - 2 * t)))
        border = border.united(
            QRegion(width - t, t, t, max(0, height - 2 * t))
        )
        self.setMask(border)
        super().resizeEvent(event)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        t = self._thickness
        width, height = self.width(), self.height()
        painter.fillRect(0, 0, width, t, self._colour)
        painter.fillRect(0, height - t, width, t, self._colour)
        painter.fillRect(0, t, t, max(0, height - 2 * t), self._colour)
        painter.fillRect(width - t, t, t, max(0, height - 2 * t), self._colour)
        painter.end()


class RegionFrame:
    """A red outline around the region being recorded.

    Usually four thin always-on-top strips, one per edge, sitting entirely
    outside the recorded rectangle.  Hyprland uses one transparent surface
    instead: four separately configured Wayland surfaces visibly jumped as
    their configure events arrived, even with compositor animation disabled.
    Its one surface paints only those same outside pixels; the centre remains
    transparent and click-through.

    It exists because taking the overlay down at the moment recording
    starts (docs/design/flow/divergences.md 4) took the scrim, the frame
    and the dimension chip with it, leaving nothing on screen that says
    what is being recorded. The report was exactly that: "i dragged to a
    region and now idk where its recording".

    Red, and the same red as the bar's border, because red means live here
    and appears nowhere else in the product.
    """

    def __init__(self, thickness: int = 3):
        self._thickness = thickness
        self._strips: list[QWidget] = []

    def is_exposed(self) -> bool:
        """Whether every strip is genuinely on screen, not merely shown.

        `show()` maps a window; the compositor puts it on screen a frame or
        so later. The caller starts a recording immediately afterwards and
        blocks the UI thread doing it, so "shown" is not the question worth
        asking -- "would the user see this yet" is.

        False for an empty frame: nothing shown is not everything shown.
        """
        if not self._strips:
            return False
        return all(
            strip.windowHandle() is not None and strip.windowHandle().isExposed()
            for strip in self._strips
        )

    def _strip(self, colour: str | None = None, alpha: float | None = None) -> QWidget:
        """One edge of the outline, or one panel of the scrim."""
        paint = design.flow_color(colour or "REC")
        if alpha is not None:
            paint.setAlphaF(alpha)
        return _Panel(paint)

    def prepare_around(self, rect, within=None, *, single_window=False) -> None:
        """Build an outline around `rect` without mapping its windows.

        Separating construction from mapping lets a Wayland platform map one
        strip at a time and identify the compositor window it created before
        the next identically-sized strip appears.  ``show_around`` remains
        the portable convenience used where Qt can place its own windows.

        `rect` uses absolute logical coordinates and the outline is drawn
        outside it.

        `within` is the screen the recording is on. Given one, the rest of
        that screen is dimmed to `SCRIM_LIVE_ALPHA` -- the handoff's live
        scrim, which the overlay used to carry before it had to come down
        (divergences.md 4). It says what is *not* being filmed, which the
        outline alone only implies.

        Only that screen. Dimming the whole virtual desktop would grey out
        the monitor the bar was deliberately placed on, and every other
        window the user still has to work with while recording.

        `single_window` deliberately ignores `within`: Hyprland gets one
        border-only surface, never screen-sized dimming windows.
        """
        self.close()
        t = self._thickness
        left, top = round(rect.left()), round(rect.top())
        width, height = round(rect.width()), round(rect.height())
        edges = [
            (left - t, top - t, width + 2 * t, t),           # above
            (left - t, top + height, width + 2 * t, t),      # below
            (left - t, top, t, height),                      # left
            (left + width, top, t, height),                  # right
        ]
        if single_window:
            # Hyprland must configure one compositor surface, not a stack
            # of screen-sized dim panels.  Those panels can cover controls,
            # steal focus and make the selected region appear to move.
            outline = _OutlinePanel(design.flow_color("REC"), t)
            outline.setGeometry(left - t, top - t, width + 2 * t, height + 2 * t)
            self._strips.append(outline)
            return

        if within is not None:
            # Four panels covering `within` minus `rect`, so the recorded
            # area is the one part of that screen at full brightness.
            sl, st = round(within.left()), round(within.top())
            sr, sb = round(within.right()), round(within.bottom())
            for dim in (
                (sl, st, sr - sl, top - st),                      # above
                (sl, top + height, sr - sl, sb - (top + height)),  # below
                (sl, top, left - sl, height),                     # left
                (left + width, top, sr - (left + width), height),  # right
            ):
                x, y, w, h = dim
                if w <= 0 or h <= 0:
                    continue
                panel = self._strip("SCRIM", tokens.FlowColor.SCRIM_LIVE_ALPHA)
                panel.setGeometry(x, y, w, h)
                self._strips.append(panel)

        for x, y, w, h in edges:
            strip = self._strip()
            strip.setGeometry(x, y, w, h)
            self._strips.append(strip)

    def show_around(self, rect, within=None) -> None:
        """Build and show the outline where Qt honours window placement."""
        self.prepare_around(rect, within=within)
        self.reveal()
        for strip in self._strips:
            strip.show()

    def reveal(self) -> None:
        """Paint every prepared edge after all its windows are in place."""
        for strip in self._strips:
            strip.reveal()

    def widgets(self) -> list[QWidget]:
        """The strips currently on screen.

        Exposed because a caller may need their native window handles --
        `platform.current.exclude_from_capture()` marks each one so the
        outline cannot film itself. This class stays a view: it hands over
        its widgets and does not know what is done with them.
        """
        return list(self._strips)

    def close(self) -> None:
        for strip in self._strips:
            strip.close()
        self._strips = []

    def is_showing(self) -> bool:
        return bool(self._strips)
