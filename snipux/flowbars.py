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

from PyQt6.QtCore import QRectF, QSize, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QFontMetricsF, QPainter, QPainterPath, QPen
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QWidget

from snipux import design
from snipux.design import tokens


def _font(size: float, weight: int, mono: bool = False) -> QFont:
    families = design.font_families()
    font = QFont(families.mono if mono else families.ui)
    font.setPixelSize(round(size))
    font.setWeight(QFont.Weight(weight))
    return font


def _glass(painter: QPainter, rect: QRectF, radius: float, *, live: bool) -> None:
    """The warm glass every bar is drawn on.

    `backdrop-filter: blur(16px)` has no Qt equivalent and the handoff says
    never to attempt a live one, so this is its documented fallback: a denser
    fill and no blur. The desktop behind is a frozen grab, so nothing moves
    under it to give the absence away.

    Alpha, not opacity -- a 93% *fill* under fully opaque children.
    `setWindowOpacity(0.93)` would wash the glyphs out with it, which the
    handoff calls out by name.

    `live` swaps the hairline for red. Red appears nowhere else in the
    product, which is exactly what lets the border alone say "recording"
    without a label; do not spend it on anything that is not live.
    """
    path = QPainterPath()
    path.addRoundedRect(rect, radius, radius)

    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(design.flow_color("BAR_BG"))
    painter.drawPath(path)

    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.setPen(
        design.flow_color("BAR_BORDER_LIVE" if live else "BAR_BORDER")
    )
    painter.drawPath(path)


class _Divider(QWidget):
    """The hairline between a bar's control groups."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setFixedSize(1, tokens.FlowMetric.DIVIDER_H)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        colour = QColor(tokens.FlowMetric.__dict__.get("DIVIDER", "#ffffff"))
        colour.setAlphaF(0.12)
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

    def set_icon(self, icon_name: str) -> None:
        self._icon_name = icon_name
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
        if not self._enabled or event.button() != Qt.MouseButton.LeftButton:
            return
        # Pressing a control then sliding off it is how a user says "no".
        if self.rect().contains(event.position().toPoint()):
            self.clicked.emit()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        metric = tokens.FlowMetric

        if self._active or (self._hovered and self._enabled):
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
        painter.end()


class _LabelledIcon(_IconButton):
    """An icon button that also says what it is set to, with a chevron.

    Used where the answer matters more than the control: "which audio
    source" has three answers and a speaker glyph gives none of them, so
    the spec draws the name beside it.
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
        self.setFixedWidth(
            round(metric.PAD + metric.ICON + 6 + width + 5 + metric.CHEVRON + metric.PAD)
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
        icon = design.icon(self._icon_name, tint)
        size = metric.ICON
        icon.paint(painter, round(x), (self.height() - size) // 2, size, size)
        x += size + 6

        painter.setFont(_font(12, 500))
        painter.setPen(tint)
        painter.drawText(
            QRectF(x, 0, self.width() - x - metric.CHEVRON - metric.PAD, self.height()),
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            self._label,
        )

        chevron = design.icon("chevron", tint)
        chevron.paint(
            painter,
            self.width() - metric.PAD - metric.CHEVRON,
            (self.height() - metric.CHEVRON) // 2,
            metric.CHEVRON,
            metric.CHEVRON,
        )
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

    Carries its own shortcut hint because the handoff's whole argument for
    the key map is that a habit should never cost a menu -- a key nobody can
    see is not a habit anyone forms.
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
        metric = tokens.FlowMetric
        text_w = self._text_width()
        glyph_w = (metric.ICON + 6) if self._glyph else 0
        self.setFixedWidth(round(metric.SPLIT_PAD_H * 2 + glyph_w + text_w))
        self.updateGeometry()
        self.update()

    def _text_width(self) -> float:
        from PyQt6.QtGui import QFontMetricsF

        width = QFontMetricsF(_font(12.5, 600)).horizontalAdvance(self._label)
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
        metric = tokens.FlowMetric

        fill = design.flow_color("REC" if self._tone == "rec" else "ACCENT")
        if self._hovered:
            fill = fill.lighter(108)
        text = design.flow_color("REC_FG" if self._tone == "rec" else "ACCENT_FG")

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(fill)
        painter.drawRoundedRect(
            QRectF(self.rect()), metric.BTN_RADIUS, metric.BTN_RADIUS
        )

        x = float(metric.SPLIT_PAD_H)
        if self._glyph:
            # Record is a filled circle rather than an icon -- the handoff is
            # specific about it, because every icon set's "record" is a
            # circle anyway and a stroked one reads as a radio button. Stop
            # is the matching square: a circle beside the word "Stop" reads
            # as record whatever the label says, which is the shape of
            # mistake this whole redesign exists to stop making.
            diameter = 10
            top = (self.height() - diameter) / 2
            painter.setBrush(text)
            if self._glyph == "square":
                painter.drawRect(QRectF(x, top, diameter, diameter))
            else:
                painter.drawEllipse(QRectF(x, top, diameter, diameter))
            x += metric.ICON + 6

        painter.setPen(text)
        painter.setFont(_font(12.5, 600))
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

    def __init__(self, rows, current: str, width: int, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowFlags(
            Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self._rows = list(rows)
        self._current = current
        self._hovered = -1
        self.setMouseTracking(True)

        metric = tokens.FlowMetric
        row_h = self._row_height()
        self.setFixedSize(width, metric.MENU_PAD * 2 + row_h * len(self._rows))

    @staticmethod
    def _row_height() -> int:
        metric = tokens.FlowMetric
        pad_v, _pad_h = metric.MENU_ROW_PAD
        return pad_v * 2 + 30

    def _row_at(self, y: float) -> int:
        metric = tokens.FlowMetric
        index = int((y - metric.MENU_PAD) // self._row_height())
        return index if 0 <= index < len(self._rows) else -1

    def open_above(self, anchor_rect) -> None:
        """Open with the menu's bottom edge above `anchor_rect`'s top.

        The audio menu opens upward so it never covers the region being
        recorded -- the one thing on screen the user is trying to look at.
        """
        metric = tokens.FlowMetric
        x = anchor_rect.center().x() - self.width() / 2
        self.move(round(x), round(anchor_rect.top() - metric.MENU_OFFSET - self.height()))
        self.show()

    def open_below(self, anchor_rect) -> None:
        metric = tokens.FlowMetric
        x = anchor_rect.center().x() - self.width() / 2
        self.move(round(x), round(anchor_rect.bottom() + metric.MENU_OFFSET))
        self.show()

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
        path = QPainterPath()
        path.addRoundedRect(surface, metric.MENU_RADIUS, metric.MENU_RADIUS)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(design.flow_color("MENU_BG"))
        painter.drawPath(path)
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
        painter.end()


class RecordingBar(QWidget):
    """The recording side of the flow, stages 3b through 6.

    Four states on one widget -- `ready`, `counting`, `live`, `done` -- and
    every control for all four is built in `__init__` and then shown or
    hidden. Rebuilding the layout per state would change the bar's width and
    therefore move both its edges, which rule 1 forbids; it is also what
    makes a control's identity survive a state change, so a click already in
    flight lands on the thing the user pressed.

    Two controls the handoff specifies are deliberately absent, both
    recorded in docs/design/flow/divergences.md 3:

    * **Pause** -- the handoff's own "Still open" does not say whether a
      paused recording is one file or segments concatenated on stop, and
      GNOME's screencast cannot pause at all, so on Linux it would have to
      be stop-and-restart. That *is* the unresolved question, so it is not
      guessed at here.
    * **Open**, on the `done` state's destination -- specified as a player
      with trim and GIF export, which the handoff says is described but not
      designed, and trimming is separately deferred.
    """

    startClicked = pyqtSignal()
    cancelClicked = pyqtSignal()
    stopClicked = pyqtSignal()
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
        self._state = self.READY
        # Both are read by `set_ready()` below, so they exist before it runs.
        self._delay_available = True
        self._last_shown: dict[str, bool] = {}

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

        self._action = _ActionButton("Record", glyph="record", shortcut="↵", parent=self)
        self._action.clicked.connect(self._on_action)
        layout.addWidget(self._action)

        self._action_divider = _Divider(self)
        layout.addSpacing(metric.GROUP_GAP - metric.GAP)
        layout.addWidget(self._action_divider)
        layout.addSpacing(metric.GROUP_GAP - metric.GAP)

        # Labelled, because "which audio source" is a question with three
        # answers and a speaker glyph answers none of them -- the spec
        # draws "System" beside it. `_LabelledIcon` keeps the chevron, so
        # it still reads as something that opens.
        self._audio = _LabelledIcon("mute", "Muted", self)
        self._audio.clicked.connect(self.audioClicked)
        layout.addWidget(self._audio)

        self._delay = _IconButton("timer", self)
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
        self._action.set_label("Record", shortcut="↵", glyph="circle")
        self._action.set_tone("accent")
        self._show(action=True, clock=False, audio=True,
                   delay=self._delay_available,
                   summary=False, cancel=True, discard=False)

    def set_counting(self, seconds: int) -> None:
        """Stage 4. The numeral itself goes inside the region -- see
        `CountdownNumeral` -- so the bar reduces to an armed pill and a way
        out, and nothing here restates the count.
        """
        self._state = self.COUNTING
        self._action.set_label(f"Starting in {seconds}", shortcut="", glyph="circle")
        self._action.set_tone("accent")
        self._show(action=True, clock=False, audio=False, delay=False,
                   summary=False, cancel=True, discard=False)

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
        self._action.set_tone("rec")
        self._show(action=True, clock=True, audio=True, delay=False,
                   summary=bool(size), cancel=False, discard=False)

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
        self._show(action=destination is not None, clock=False, audio=False,
                   delay=False, summary=True, cancel=False, discard=True)

    # -- audio ---------------------------------------------------------
    def set_audio(self, source: str) -> None:
        """Reflect the chosen source. The bar renders it; whether a source is
        even offerable is the platform's business, not this widget's.
        """
        chosen = {
            identifier: (icon, label)
            for identifier, icon, label, _note in tokens.AUDIO_SOURCES
        }.get(source, ("mute", "Muted"))
        self._audio.set_content(*chosen)

    def set_audio_enabled(self, enabled: bool) -> None:
        self._audio.set_enabled(enabled)

    def delay_control(self) -> QWidget:
        """The delay button, for a menu to anchor itself against."""
        return self._delay

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
        head = visible.get("action", False) or visible.get("clock", False)
        middle = (
            visible.get("audio", False)
            or visible.get("delay", False)
            or visible.get("summary", False)
        )
        tail = visible.get("cancel", False) or visible.get("discard", False)
        self._action_divider.setVisible(head and (middle or tail))
        self._tail_divider.setVisible(tail and (head or middle))

        self.adjustSize()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        _glass(painter, rect, tokens.FlowMetric.RADIUS, live=self._state == self.LIVE)
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
        self._colour = colour
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowTransparentForInput
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        if colour.alphaF() < 1.0:
            self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), self._colour)
        painter.end()


class RegionFrame:
    """A red outline around the region being recorded.

    Not a widget: **four** thin always-on-top strips, one per edge, sitting
    entirely *outside* the recorded rectangle. One window covering the
    region with a transparent middle would be simpler and is the obvious
    thing to reach for -- and it puts a window over the very pixels being
    filmed, which is a question about compositing this project cannot yet
    answer on Wayland. Four strips make it a non-question: nothing this
    draws is ever inside the frame.

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

    def _strip(self, colour: str | None = None, alpha: float | None = None) -> QWidget:
        """One edge of the outline, or one panel of the scrim."""
        paint = design.flow_color(colour or "REC")
        if alpha is not None:
            paint.setAlphaF(alpha)
        return _Panel(paint)

    def show_around(self, rect, within=None) -> None:
        """Outline `rect` (absolute logical coordinates), drawn outside it.

        `within` is the screen the recording is on. Given one, the rest of
        that screen is dimmed to `SCRIM_LIVE_ALPHA` -- the handoff's live
        scrim, which the overlay used to carry before it had to come down
        (divergences.md 4). It says what is *not* being filmed, which the
        outline alone only implies.

        Only that screen. Dimming the whole virtual desktop would grey out
        the monitor the bar was deliberately placed on, and every other
        window the user still has to work with while recording.
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
                panel.show()
                self._strips.append(panel)

        for x, y, w, h in edges:
            strip = self._strip()
            strip.setGeometry(x, y, w, h)
            strip.show()
            self._strips.append(strip)

    def close(self) -> None:
        for strip in self._strips:
            strip.close()
        self._strips = []

    def is_showing(self) -> bool:
        return bool(self._strips)
