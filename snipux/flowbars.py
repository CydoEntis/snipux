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
and `AppController` decides what that means, the same split `RecordingHud`
already used -- which is what lets the audio menu, the destination menu and
the platform's own opinion about what is possible live in one place instead
of three.
"""

from __future__ import annotations

from PyQt6.QtCore import QRectF, QSize, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
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

    def set_label(self, label: str, *, shortcut: str | None = None) -> None:
        self._label = label
        if shortcut is not None:
            self._shortcut = shortcut
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
            # Record is a filled circle, not a glyph -- the handoff is
            # specific about this, because every icon set's "record" is a
            # circle anyway and a stroked one reads as a radio button.
            diameter = 10
            painter.setBrush(text)
            painter.drawEllipse(
                QRectF(x, (self.height() - diameter) / 2, diameter, diameter)
            )
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
        self.setFont(_font(12.5, 600, mono=True))
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._apply()

    def set_token(self, token: str) -> None:
        self._token = token
        self._apply()

    def _apply(self) -> None:
        self.setStyleSheet(f"color: {design.flow_color(self._token).name()};")


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
        # `RecordingHud` use.
        super().__init__(parent)
        metric = tokens.FlowMetric
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self._state = self.READY

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

        self._action = _ActionButton("Record", glyph="record", shortcut="↵", parent=self)
        self._action.clicked.connect(self._on_action)
        layout.addWidget(self._action)

        self._clock = _Readout(self, token="REC_CLOCK")
        layout.addWidget(self._clock)

        self._action_divider = _Divider(self)
        layout.addSpacing(metric.GROUP_GAP - metric.GAP)
        layout.addWidget(self._action_divider)
        layout.addSpacing(metric.GROUP_GAP - metric.GAP)

        self._audio = _IconButton("mute", self)
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

        self._cancel = _IconButton("close", self)
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
        self._action.set_label("Record", shortcut="↵")
        self._action.set_tone("accent")
        self._show(action=True, clock=False, audio=True, delay=True,
                   summary=False, cancel=True, discard=False)

    def set_counting(self, seconds: int) -> None:
        """Stage 4. The numeral itself goes inside the region -- see
        `CountdownNumeral` -- so the bar reduces to an armed pill and a way
        out, and nothing here restates the count.
        """
        self._state = self.COUNTING
        self._action.set_label(f"Starting in {seconds}", shortcut="")
        self._action.set_tone("accent")
        self._show(action=True, clock=False, audio=False, delay=False,
                   summary=False, cancel=True, discard=False)

    def set_live(self, elapsed: str, *, size: str = "") -> None:
        """Stage 5. The clock leads, then Stop.

        The clock sits *before* the action rather than after it, which is the
        one place this bar departs from "action at the left end": while
        recording, elapsed time is the thing being watched, and rule 3 is
        about the primary action never moving when something to its right
        changes -- a clock that pushed Stop sideways every second would break
        the rule it was obeying.
        """
        self._state = self.LIVE
        self._clock.setText(elapsed)
        self._summary.setText(size)
        self._action.set_label("Stop", shortcut="")
        self._action.set_tone("rec")
        self._show(action=True, clock=True, audio=True, delay=False,
                   summary=bool(size), cancel=False, discard=False)

    def set_done(self, summary: str, *, destination: str = "Copy") -> None:
        """Stage 6. The destination becomes the action; the summary says what
        was produced, and discard is the way to decide it was not worth
        keeping.
        """
        self._state = self.DONE
        self._action.set_label(destination, shortcut="↵")
        self._action.set_tone("accent")
        self._summary.setText(summary)
        self._show(action=True, clock=False, audio=False, delay=False,
                   summary=True, cancel=False, discard=True)

    # -- audio ---------------------------------------------------------
    def set_audio(self, source: str) -> None:
        """Reflect the chosen source. The bar renders it; whether a source is
        even offerable is the platform's business, not this widget's.
        """
        glyph = dict(
            (identifier, icon) for identifier, icon, _label, _note in tokens.AUDIO_SOURCES
        ).get(source, "mute")
        self._audio.set_icon(glyph)

    def set_audio_enabled(self, enabled: bool) -> None:
        self._audio.set_enabled(enabled)

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

    def _show(self, **visible: bool) -> None:
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
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
        )
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
