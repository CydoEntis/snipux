"""The Settings window.

`docs/design/handoff-windows.md` section 2 is the authority. 780 x 580, a
182px nav rail, four panes, a 56px footer. Not a modal dialog -- the user
may well want it open while they test a snip.

`setup_desktop.py` is still where a shortcut is validated, stored and bound,
and it deliberately imports no Qt: `--setup` runs with no display. This
module is the Qt in front of it, and goes through those same functions, so
the CLI and the window can never disagree about what a valid shortcut is.

Nothing applies live. Save commits the lot; the caller's `on_saved` callback
(`app.py`'s `_on_settings_saved`) is what actually rebinds the shortcut,
platform seam and all -- this window only decides *what* to save.

The conflict check itself is platform-specific (SNX-93): GNOME's custom
keybindings are introspectable, so `ConflictBanner` can list every schema
already bound to a combination by name (`setup_desktop.
find_shortcut_conflicts_named`); Windows has no such registry, so
`platform.current.find_shortcut_conflict()` -- the Windows Snipping Tool's
own Win+Shift+S, plus an actual `RegisterHotKey` probe for anything else
holding the key -- stands in instead, and Save refuses a taken combination
outright rather than appearing to succeed and silently leaving the old one
bound (see `_save()`). Both paths branch on `HotkeyEventFilter.
is_available()`, the same capability check `app.py`'s own
platform-dependent paths already use, not `sys.platform` directly.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from PyQt6.QtCore import QPoint, QRectF, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import (
    QColor,
    QFont,
    QFontDatabase,
    QIcon,
    QImage,
    QImageReader,
    QKeyEvent,
    QKeySequence,
    QPainter,
    QPen,
    QPixmap,
)
from PyQt6.QtWidgets import (
    QButtonGroup,
    QColorDialog,
    QComboBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QSpinBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from . import design, platform, player, setup_desktop
from .shapes import Watermark
from .design import tokens
from .platform.windows import HotkeyEventFilter
from .winchrome import (
    AccentButton,
    scrollbar_style,
    SecondaryButton,
    SectionHeading,
    Switch,
    WinWindow,
    _mono_font,
    _ui_font,
)

# Qt modifier -> the token used in the normalised name. Order is fixed at
# Control, Alt, Shift, Super by the design, because this exact string is what
# both the conflict check and gsettings are keyed on -- a permutation would
# quietly miss a real clash.
_MODIFIER_TOKENS = (
    (Qt.KeyboardModifier.ControlModifier, "Control"),
    (Qt.KeyboardModifier.AltModifier, "Alt"),
    (Qt.KeyboardModifier.ShiftModifier, "Shift"),
    (Qt.KeyboardModifier.MetaModifier, "Super"),
)

# Pressed alone these are the user still reaching for a combination, not a
# combination. Recording ignores them rather than committing on the first
# modifier down.
_BARE_MODIFIER_KEYS = frozenset(
    {
        Qt.Key.Key_Control,
        Qt.Key.Key_Alt,
        Qt.Key.Key_AltGr,
        Qt.Key.Key_Shift,
        Qt.Key.Key_Meta,
        Qt.Key.Key_Super_L,
        Qt.Key.Key_Super_R,
        Qt.Key.Key_CapsLock,
        Qt.Key.Key_NumLock,
        Qt.Key.Key_ScrollLock,
    }
)


def accelerator_from_event(event: QKeyEvent) -> str | None:
    """A key press -> the design's normalised name (`Control+Alt+S`), or None
    if it is not a shortcut yet.

    None means "keep listening": a bare modifier, an unnameable key, or a
    combination with no modifier at all -- the design requires at least one,
    since a bare letter would swallow that key desktop-wide.
    """
    key = event.key()
    if key in _BARE_MODIFIER_KEYS or key == Qt.Key.Key_unknown:
        return None

    name = QKeySequence(key).toString()
    if not name:
        return None
    if len(name) == 1:
        name = name.upper()

    modifiers = event.modifiers()
    tokens_found = [token for flag, token in _MODIFIER_TOKENS if modifiers & flag]
    if not tokens_found:
        return None
    return "+".join(tokens_found + [name])


class ShortcutRecorder(QWidget):
    """The 38px field plus its Record button.

    A plain text input would mean typing the accelerator by hand, which is
    the syntax this window exists to hide.
    """

    recorded = pyqtSignal(str)

    _PULSE_MS = 550  # half of the design's 1.1s opacity cycle

    def __init__(self, shortcut: str, parent: QWidget | None = None):
        super().__init__(parent)
        self._shortcut = shortcut
        self._recording = False
        self._dot_on = True

        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)

        self._field = QLabel()
        self._field.setFixedHeight(tokens.WinMetric.RECORDER_H)
        self._field.setFont(_mono_font(13))
        self._field.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        row.addWidget(self._field, 1)

        self._button = SecondaryButton("Record")
        self._button.clicked.connect(self._toggle)
        row.addWidget(self._button)

        # Rendered only while recording, per the design -- not a permanently
        # present dot faded in and out.
        self._pulse = QTimer(self)
        self._pulse.setInterval(self._PULSE_MS)
        self._pulse.timeout.connect(self._blink)

        self._refresh()

    def shortcut_value(self) -> str:
        return self._shortcut

    def set_shortcut(self, shortcut: str) -> None:
        self._shortcut = shortcut
        self._refresh()

    def is_recording(self) -> bool:
        return self._recording

    def _refresh(self) -> None:
        win = tokens.Win
        if self._recording:
            dot = tokens.Color.ACCENT if self._dot_on else tokens.Win.TEXT_DISABLED
            self._field.setText(
                f'<span style="color:{dot};">●</span>'
                f'&nbsp;&nbsp;<span style="color:{win.TEXT_MUTED};">'
                "Press a combination…</span>"
            )
            border, fill = tokens.Color.ACCENT, "#22262d"
        else:
            self._field.setText(
                f'<span style="color:{win.TEXT_PRIMARY};">{self._shortcut}</span>'
            )
            border, fill = win.FIELD_BORDER, win.FIELD_BG
        self._field.setStyleSheet(
            f"background: {fill}; border: 1px solid {border};"
            f" border-radius: {tokens.WinMetric.CONTROL_RADIUS}px; padding: 0 11px;"
        )
        self._button.setText("Cancel" if self._recording else "Record")

    def _blink(self) -> None:
        self._dot_on = not self._dot_on
        self._refresh()

    def _toggle(self) -> None:
        if self._recording:
            self._stop()
        else:
            self._start()

    def _start(self) -> None:
        self._recording = True
        self._dot_on = True
        # Grabbed for the duration so the combination being recorded does not
        # also fire whatever currently owns it.
        self.grabKeyboard()
        self.setFocus(Qt.FocusReason.OtherFocusReason)
        self._pulse.start()
        self._refresh()

    def _stop(self) -> None:
        self._recording = False
        self._pulse.stop()
        self.releaseKeyboard()
        self._refresh()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if not self._recording:
            super().keyPressEvent(event)
            return
        if event.key() == Qt.Key.Key_Escape and not event.modifiers():
            # Cancels and keeps the old binding: someone who opened the
            # recorder by accident needs a way out that changes nothing.
            self._stop()
            return
        accelerator = accelerator_from_event(event)
        if accelerator is None:
            return
        self._shortcut = accelerator
        self._stop()
        self.recorded.emit(accelerator)


def _rgba(token_name: str) -> str:
    """A `Win` colour+alpha pair as a stylesheet rgba() string.

    Goes through `design.win_color` rather than re-typing the percentage,
    so the alpha in the stylesheet and the alpha in tokens.py cannot drift.
    """
    colour = design.win_color(token_name)
    return (
        f"rgba({colour.red()}, {colour.green()}, {colour.blue()}, "
        f"{colour.alphaF():.2f})"
    )


class ConflictBanner(QLabel):
    """The clear/clash box directly under the recorder.

    Under the field rather than in a tooltip or a dialog on Save, because a
    clash is information about the choice being made right now.

    The check behind it is platform-specific -- see this module's own
    docstring for why -- and branches on `HotkeyEventFilter.is_available()`
    rather than `sys.platform`, so it holds regardless of which OS actually
    runs a test for it.
    """

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWordWrap(True)
        self.setFont(_ui_font(11.5, 400))

    def show_for(self, shortcut: str) -> None:
        win = tokens.Win
        if HotkeyEventFilter.is_available():
            holder = platform.current.find_shortcut_conflict(shortcut)
            if holder is not None:
                text = f"✕  {shortcut} is already used by {holder}."
                fg = win.ERR_FG
                bg, border = _rgba("ERR_BG"), _rgba("ERR_BORDER")
            else:
                text = f"✓  {shortcut} is free to register."
                fg = win.OK_FG
                bg, border = _rgba("OK_BG"), _rgba("OK_BORDER")
        else:
            conflicts = setup_desktop.find_shortcut_conflicts_named(shortcut)
            if conflicts:
                owner = conflicts[0][1]
                text = (
                    f"✕  {shortcut} is already {owner}. GNOME will not warn "
                    "you — it will just fire the wrong one."
                )
                fg = win.ERR_FG
                bg, border = _rgba("ERR_BG"), _rgba("ERR_BORDER")
            else:
                text = f"✓  No GNOME shortcut uses {shortcut}."
                fg = win.OK_FG
                bg, border = _rgba("OK_BG"), _rgba("OK_BORDER")
        self.setText(text)
        self.setStyleSheet(
            f"color: {fg}; background: {bg}; border: 1px solid {border};"
            " border-radius: 8px; padding: 9px 11px;"
        )


class _RadioRing(QWidget):
    """The 15px ring with its 7px dot.

    Painted rather than stylesheet'd: a ring with a centred dot is two
    concentric circles, and the qradialgradient needed to fake that in CSS
    renders as a soft blob at this size.
    """

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._selected = False
        self.setFixedSize(tokens.WinMetric.RADIO_D, tokens.WinMetric.RADIO_D)

    def set_selected(self, selected: bool) -> None:
        self._selected = selected
        self.update()

    def paintEvent(self, event) -> None:
        from PyQt6.QtCore import QRectF
        from PyQt6.QtGui import QColor, QPainter

        metric = tokens.WinMetric
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        ring = QColor(tokens.Color.ACCENT if self._selected else "#4a505b")
        painter.setPen(ring)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawEllipse(QRectF(self.rect()).adjusted(0.75, 0.75, -0.75, -0.75))
        if self._selected:
            inset = (metric.RADIO_D - metric.RADIO_DOT) / 2
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(tokens.Color.ACCENT))
            painter.drawEllipse(
                QRectF(inset, inset, metric.RADIO_DOT, metric.RADIO_DOT)
            )
        painter.end()


class RadioCard(QPushButton):
    """One of the three mutually exclusive "After capture" behaviours.

    Cards rather than a checkbox: opening a review window is one of three
    real behaviours, and a checkbox hid the other two entirely.

    A QPushButton hosting a layout does not size itself from that layout --
    its own sizeHint is for a text label it does not have -- so the height
    is taken from the layout explicitly, or the card collapses to a sliver
    with its contents clipped away.

    `set_unavailable` greys a card whose behaviour cannot run on this
    machine at all -- the Recording pane's GIF row, when there is no
    ffmpeg to convert with.
    """

    def __init__(self, label: str, note: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        row = QHBoxLayout(self)
        row.setContentsMargins(
            tokens.WinMetric.CARD_PAD[1], tokens.WinMetric.CARD_PAD[0],
            tokens.WinMetric.CARD_PAD[1], tokens.WinMetric.CARD_PAD[0],
        )
        row.setSpacing(11)

        self._ring = _RadioRing()
        row.addWidget(self._ring, 0, Qt.AlignmentFlag.AlignTop)

        text = QVBoxLayout()
        text.setSpacing(3)
        self._title = QLabel(label)
        self._title.setFont(_ui_font(12.5, 500))
        text.addWidget(self._title)
        self._sub = None
        if note:
            self._sub = QLabel(note)
            self._sub.setFont(_ui_font(11.5, 400))
            self._sub.setWordWrap(True)
            self._sub.setStyleSheet(f"color: {tokens.Win.TEXT_NOTE}; background: transparent;")
            text.addWidget(self._sub)
        row.addLayout(text, 1)

        self._unavailable_reason = ""
        self.setMinimumHeight(row.sizeHint().height())
        self.toggled.connect(lambda _checked: self._refresh())
        self._refresh()

    def set_unavailable(self, reason: str) -> None:
        """Grey the card and swap its note for `reason`, exactly as the
        player's own export menu greys a row it cannot write -- a missing
        row reads as a bug, a greyed one with a reason reads as a limit.

        Unchecked and disabled rather than merely dimmed: a card the user
        cannot pick must not also be the one `QButtonGroup.checkedId()`
        reports as selected.
        """
        self._unavailable_reason = reason
        self.setChecked(False)
        self.setEnabled(False)
        if self._sub is not None:
            self._sub.setText(reason)
        self._refresh()

    def _refresh(self) -> None:
        selected = self.isChecked()
        unavailable = bool(self._unavailable_reason)
        self._ring.set_selected(selected)
        title_colour = (
            tokens.Win.TEXT_FAINT if unavailable
            else tokens.Win.TEXT_PRIMARY if selected
            else tokens.Win.TEXT_BODY
        )
        self._title.setStyleSheet(f"color: {title_colour}; background: transparent;")
        if self._sub is not None and unavailable:
            self._sub.setStyleSheet(f"color: {tokens.Win.TEXT_FAINT}; background: transparent;")
        background = "#1e2229" if selected else "transparent"
        border = tokens.Win.CONTROL_BORDER_HOVER if selected else tokens.Win.SEGMENT_BORDER
        self.setStyleSheet(
            f"QPushButton {{ text-align: left; border-radius: 9px;"
            f" background: {background}; border: 1px solid {border}; }}"
            f"QPushButton:hover {{ background: {tokens.Win.ROW_HOVER}; }}"
        )


class SwitchRow(QWidget):
    """Label, optional note, and a switch pushed to the right."""

    def __init__(self, label: str, note: str = "", checked: bool = False, parent=None):
        super().__init__(parent)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(12)

        text = QVBoxLayout()
        text.setSpacing(2)
        title = QLabel(label)
        title.setFont(_ui_font(12.5, 500))
        title.setStyleSheet(f"color: {tokens.Win.TEXT_BODY};")
        text.addWidget(title)
        if note:
            sub = QLabel(note)
            sub.setFont(_ui_font(11.5, 400))
            sub.setWordWrap(True)
            sub.setStyleSheet(f"color: {tokens.Win.TEXT_NOTE};")
            text.addWidget(sub)
        row.addLayout(text, 1)

        self.switch = Switch()
        self.switch.setChecked(checked)
        row.addWidget(self.switch, 0, Qt.AlignmentFlag.AlignTop)


# Every control on the right of a `_ControlRow` is this wide, so the column
# of them lines up down the pane.
_CONTROL_W = 220

# How "each tool's own" colour is drawn: a pie of the colours the tools ship
# with, since it is not one colour.
_EACH_TOOLS_OWN = ["#e3ff4f", "#ef4444", "#38bdf8", "#facc15"]


class _ControlRow(QWidget):
    """Label, optional note, and any control pushed to the right -- the
    general form of `SwitchRow` and `SpinRow`."""

    def __init__(self, label: str, note: str, control: QWidget, parent=None):
        super().__init__(parent)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(12)

        text = QVBoxLayout()
        text.setSpacing(2)
        title = QLabel(label)
        title.setFont(_ui_font(12.5, 500))
        title.setStyleSheet(f"color: {tokens.Win.TEXT_BODY};")
        text.addWidget(title)
        if note:
            sub = QLabel(note)
            sub.setFont(_ui_font(11.5, 400))
            sub.setWordWrap(True)
            sub.setStyleSheet(f"color: {tokens.Win.TEXT_NOTE};")
            text.addWidget(sub)
        # A bare label centres on its control; one with a note under it
        # tops out level with the control, as `SwitchRow`'s does.
        align = Qt.AlignmentFlag.AlignTop if note else Qt.AlignmentFlag.AlignVCenter
        row.addLayout(text, 1)
        row.setAlignment(text, align)

        self.control = control
        row.addWidget(control, 0, align)


def _dropdown_style(padding_left: int) -> str:
    """A button that reads as a field: the text field's own ground, with
    room at the right for the chevron `paintEvent` draws."""
    win, metric = tokens.Win, tokens.WinMetric
    return (
        f"QPushButton {{ text-align: left; background: {win.FIELD_BG};"
        f" border: 1px solid {win.FIELD_BORDER};"
        f" border-radius: {metric.CONTROL_RADIUS}px; color: {win.TEXT_PRIMARY};"
        f" padding: 0 30px 0 {padding_left}px; }}"
        f"QPushButton:hover {{ border-color: {win.CONTROL_BORDER_HOVER}; }}"
    )


def _paint_chevron(button: QPushButton) -> None:
    painter = QPainter(button)
    size = 12
    design.icon("chevron", tokens.Win.ICON_IDLE).paint(
        painter, button.width() - size - 11, (button.height() - size) // 2, size, size
    )
    painter.end()


def _menu_style() -> str:
    win, metric = tokens.Win, tokens.WinMetric
    return (
        f"QMenu {{ background: {win.CHROME_BG}; border: 1px solid {win.CONTROL_BORDER_HOVER};"
        f" border-radius: {metric.CONTROL_RADIUS}px; padding: 4px; }}"
        f"QMenu::item {{ color: {win.TEXT_SECONDARY}; padding: 6px 16px 6px 8px;"
        f" border-radius: 6px; }}"
        f"QMenu::item:selected {{ background: {win.ROW_HOVER}; color: {win.TEXT_PRIMARY}; }}"
        f"QMenu::icon {{ padding-left: 8px; }}"
    )


class Select(QPushButton):
    """A dropdown in this window's own styling: the choice's glyph and name
    on a field, a chevron at the end, and a menu of every option.

    Not a `QComboBox`: its arrow and its popup list come from the platform
    style, which draws a light system list under a dark window.

    `options` are `(value, label, glyph or None)`. `changed` fires on a
    pick that changes the value, never on `set_value`.
    """

    changed = pyqtSignal(str)

    def __init__(self, options, current: str, parent: QWidget | None = None):
        super().__init__(parent)
        self._options = list(options)
        values = [value for value, _label, _glyph in self._options]
        self._value = current if current in values else values[0]
        self._menu: QMenu | None = None
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFont(_ui_font(12.5, 500))
        self.setFixedSize(_CONTROL_W, tokens.WinMetric.CONTROL_H)
        self.setIconSize(QSize(15, 15))
        self.setStyleSheet(_dropdown_style(10))
        self.clicked.connect(self._open)
        self._refresh()

    def value(self) -> str:
        return self._value

    def values(self) -> list[str]:
        return [value for value, _label, _glyph in self._options]

    def set_value(self, value: str) -> None:
        if value in self.values():
            self._value = value
            self._refresh()

    def menu_widget(self) -> QMenu | None:
        """The menu the last click opened, or None before one has."""
        return self._menu

    def _refresh(self) -> None:
        label, glyph = next(
            (label, glyph) for value, label, glyph in self._options if value == self._value
        )
        self.setText(label)
        self.setIcon(design.icon(glyph, tokens.Win.TEXT_SECONDARY) if glyph else QIcon())

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        _paint_chevron(self)

    def _open(self) -> None:
        win = tokens.Win
        menu = QMenu(self)
        menu.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        menu.setStyleSheet(_menu_style())
        menu.setFont(_ui_font(12.5, 500))
        menu.setMinimumWidth(self.width())
        for value, label, glyph in self._options:
            action = menu.addAction(label)
            current = value == self._value
            if glyph:
                action.setIcon(design.icon(glyph, win.ICON_ACTIVE if current else win.ICON_IDLE))
            if current:
                # The current choice is the bold one: a tick column would
                # push every label right for the sake of one row.
                font = _ui_font(12.5, 600)
                action.setFont(font)
            action.triggered.connect(lambda _checked=False, v=value: self._choose(v))
        self._menu = menu
        menu.popup(self.mapToGlobal(QPoint(0, self.height() + 4)))

    def _choose(self, value: str) -> None:
        if value == self._value:
            return
        self._value = value
        self._refresh()
        self.changed.emit(value)


def _swatch_name(colour: str) -> str:
    """The swatch's own name for `colour`, or the hex where it has none."""
    for name, hex_colour in tokens.SETTINGS_SWATCHES:
        if hex_colour == colour:
            return name
    return colour.upper()


def _paint_dot(painter: QPainter, rect: QRectF, colours: list[str]) -> None:
    """A colour dot, or -- for more than one colour -- a pie of them, which is
    how "each tool's own" is drawn: several colours at once, not one."""
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    if len(colours) == 1:
        painter.setBrush(QColor(colours[0]))
        painter.drawEllipse(rect)
    else:
        span = 360 * 16 // len(colours)
        for index, colour in enumerate(colours):
            painter.setBrush(QColor(colour))
            painter.drawPie(rect, 90 * 16 - index * span, -span)
    # A hairline ring, so the near-black swatch still has an edge against
    # the dark field.
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.setPen(QPen(QColor(255, 255, 255, 60), 1))
    painter.drawEllipse(rect)


class _Swatch(QPushButton):
    """One colour in `_ColourPopup`: a disc, ringed when it is the choice."""

    def __init__(self, name: str, colour: str, selected: bool, parent=None):
        super().__init__(parent)
        self.colour = colour
        self._selected = selected
        self.setFixedSize(24, 24)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip(f"{name} · {colour.upper()}")
        self.setStyleSheet("QPushButton { border: none; background: transparent; }")

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        _paint_dot(painter, QRectF(4, 4, 16, 16), [self.colour])
        if self._selected or self.underMouse():
            painter.setBrush(Qt.BrushStyle.NoBrush)
            ring = tokens.Win.ICON_ACTIVE if self._selected else tokens.Win.ICON_IDLE
            painter.setPen(QPen(QColor(ring), 1.5))
            painter.drawEllipse(QRectF(1, 1, 22, 22))
        painter.end()


class _ColourPopup(QFrame):
    """What `ColourPicker` opens: the "no colour of its own" choice, the
    swatches, and a hex field for anything else."""

    chosen = pyqtSignal(object)

    def __init__(self, current: str | None, inherit_label: str, inherit_colours, parent=None):
        super().__init__(parent)
        win, metric = tokens.Win, tokens.WinMetric
        self.setWindowFlags(Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint)
        # Translucent so the corners can be round; the ground is painted by
        # `paintEvent`, since a translucent top-level paints no stylesheet
        # background of its own.
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        column = QVBoxLayout(self)
        column.setContentsMargins(8, 8, 8, 8)
        column.setSpacing(8)

        self.inherit = QPushButton(inherit_label)
        self.inherit.setCursor(Qt.CursorShape.PointingHandCursor)
        self.inherit.setFont(_ui_font(12.5, 600 if current is None else 500))
        self.inherit.setIcon(QIcon(_dot_pixmap(inherit_colours)))
        self.inherit.setIconSize(QSize(14, 14))
        self.inherit.setStyleSheet(
            f"QPushButton {{ text-align: left; border: none; border-radius: 6px;"
            f" padding: 6px 8px; color: {win.TEXT_PRIMARY if current is None else win.TEXT_SECONDARY};"
            f" background: {win.SELECTED_BG if current is None else 'transparent'}; }}"
            f"QPushButton:hover {{ background: {win.ROW_HOVER}; }}"
        )
        self.inherit.clicked.connect(lambda _c=False: self._choose(None))
        column.addWidget(self.inherit)

        swatches = QHBoxLayout()
        swatches.setContentsMargins(0, 0, 0, 0)
        swatches.setSpacing(2)
        self.swatches: list[_Swatch] = []
        for name, colour in tokens.SETTINGS_SWATCHES:
            swatch = _Swatch(name, colour, colour == current)
            swatch.clicked.connect(lambda _c=False, c=colour: self._choose(c))
            swatches.addWidget(swatch)
            self.swatches.append(swatch)
        column.addLayout(swatches)

        self.hex = QLineEdit(current.upper() if current else "")
        self.hex.setPlaceholderText("#RRGGBB")
        self.hex.setMaxLength(7)
        self.hex.setFont(_mono_font(12))
        self.hex.setFixedHeight(28)
        self.hex.setStyleSheet(_field_style())
        self.hex.returnPressed.connect(self._take_hex)
        column.addWidget(self.hex)

    def paintEvent(self, event) -> None:
        win, metric = tokens.Win, tokens.WinMetric
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor(win.CONTROL_BORDER_HOVER), 1))
        painter.setBrush(QColor(win.CHROME_BG))
        radius = metric.CONTROL_RADIUS
        painter.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), radius, radius)
        painter.end()

    def _take_hex(self) -> None:
        text = self.hex.text().strip()
        if not text.startswith("#"):
            text = f"#{text}"
        colour = setup_desktop._hex_or_none(text)
        if colour is None:
            # Said where it was typed, not in a dialog over a popup.
            self.hex.setStyleSheet(
                _field_style() + f"QLineEdit {{ border-color: {tokens.Win.CLOSE_HOVER}; }}"
            )
            return
        self._choose(colour)

    def _choose(self, colour: str | None) -> None:
        self.chosen.emit(colour)
        self.close()


def _dot_pixmap(colours) -> QPixmap:
    pixmap = QPixmap(28, 28)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    _paint_dot(painter, QRectF(1, 1, 26, 26), list(colours))
    painter.end()
    return pixmap


class ColourPicker(QPushButton):
    """A colour, or no colour of its own (`inherit_label`, drawn with
    `inherit_colours`), on a field that opens `_ColourPopup`.

    `changed` carries the new value -- a `#rrggbb`, or None -- on a pick
    that changes it, never on `set_value`.
    """

    changed = pyqtSignal(object)

    def __init__(
        self,
        current: str | None,
        inherit_label: str,
        inherit_colours,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self._value = current
        self._inherit_label = inherit_label
        self._inherit_colours = list(inherit_colours)
        self._popup: _ColourPopup | None = None
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFont(_ui_font(12.5, 500))
        self.setFixedSize(_CONTROL_W, tokens.WinMetric.CONTROL_H)
        self.setStyleSheet(_dropdown_style(34))
        self.clicked.connect(self._open)
        self._refresh()

    def value(self) -> str | None:
        return self._value

    def set_value(self, value: str | None) -> None:
        self._value = value
        self._refresh()

    def set_inherit(self, label: str, colours) -> None:
        """What choosing no colour of its own means just now -- for a
        tool's colour, whatever the default ink above it is."""
        self._inherit_label = label
        self._inherit_colours = list(colours)
        self._refresh()

    def popup_widget(self) -> "_ColourPopup | None":
        """The popup the last click opened, or None before one has."""
        return self._popup

    def _refresh(self) -> None:
        self.setText(self._inherit_label if self._value is None else _swatch_name(self._value))
        self.update()

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        painter = QPainter(self)
        colours = self._inherit_colours if self._value is None else [self._value]
        size = 14
        _paint_dot(painter, QRectF(12, (self.height() - size) / 2, size, size), colours)
        painter.end()
        _paint_chevron(self)

    def _open(self) -> None:
        popup = _ColourPopup(self._value, self._inherit_label, self._inherit_colours)
        popup.chosen.connect(self._choose)
        popup.setFixedWidth(max(self.width(), popup.sizeHint().width()))
        popup.move(self.mapToGlobal(QPoint(0, self.height() + 4)))
        popup.show()
        self._popup = popup

    def _choose(self, colour: str | None) -> None:
        if colour == self._value:
            return
        self._value = colour
        self._refresh()
        self.changed.emit(colour)


class SliderControl(QWidget):
    """A bounded number as a slider with its value beside it -- the style
    popover's own control for stroke and strength, in this window's
    styling."""

    changed = pyqtSignal(int)

    def __init__(self, value: int, bounds: tuple[int, int], suffix: str = "", parent=None):
        super().__init__(parent)
        win = tokens.Win
        self._suffix = suffix
        self.setFixedSize(_CONTROL_W, tokens.WinMetric.CONTROL_H)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(10)
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(*bounds)
        self.slider.setValue(value)
        self.slider.setCursor(Qt.CursorShape.PointingHandCursor)
        self.slider.setStyleSheet(
            f"QSlider::groove:horizontal {{ height: 4px; background: {win.TOGGLE_OFF};"
            f" border-radius: 2px; }}"
            f"QSlider::sub-page:horizontal {{ background: {tokens.Color.ACCENT};"
            f" border-radius: 2px; }}"
            f"QSlider::handle:horizontal {{ background: {win.TEXT_PRIMARY}; width: 14px;"
            f" margin: -5px 0; border-radius: 7px; }}"
        )
        self.slider.valueChanged.connect(self._on_value)
        row.addWidget(self.slider, 1)
        self.readout = QLabel()
        self.readout.setFont(_mono_font(12))
        self.readout.setFixedWidth(40)
        self.readout.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.readout.setStyleSheet(f"color: {win.TEXT_SECONDARY};")
        row.addWidget(self.readout)
        self._show(value)

    def value(self) -> int:
        return self.slider.value()

    def set_value(self, value: int) -> None:
        self.slider.blockSignals(True)
        self.slider.setValue(value)
        self.slider.blockSignals(False)
        self._show(self.slider.value())

    def _show(self, value: int) -> None:
        self.readout.setText(f"{value}{self._suffix}")

    def _on_value(self, value: int) -> None:
        self._show(value)
        self.changed.emit(value)


class SpinRow(QWidget):
    """Label, optional note, and a bounded spin box pushed to the right --
    the numeric-control sibling of `SwitchRow`, for a setting that isn't a
    plain on/off (ticket 9's frame rate).
    """

    def __init__(
        self,
        label: str,
        note: str = "",
        value: int = 0,
        minimum: int = 1,
        maximum: int = 120,
        parent=None,
    ):
        super().__init__(parent)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(12)

        text = QVBoxLayout()
        text.setSpacing(2)
        title = QLabel(label)
        title.setFont(_ui_font(12.5, 500))
        title.setStyleSheet(f"color: {tokens.Win.TEXT_BODY};")
        text.addWidget(title)
        if note:
            sub = QLabel(note)
            sub.setFont(_ui_font(11.5, 400))
            sub.setWordWrap(True)
            sub.setStyleSheet(f"color: {tokens.Win.TEXT_NOTE};")
            text.addWidget(sub)
        row.addLayout(text, 1)

        self.spin = QSpinBox()
        self.spin.setRange(minimum, maximum)
        self.spin.setValue(value)
        self.spin.setFixedHeight(tokens.WinMetric.CONTROL_H)
        row.addWidget(self.spin, 0, Qt.AlignmentFlag.AlignTop)


class _ColorSwatch(QPushButton):
    """One colour on the watermark page's row: a rounded square, ringed
    while it is the picked one. Painted rather than styled, as the overlay's
    ink swatches are, because a stylesheet cannot draw the ring outside the
    colour with a gap between."""

    def __init__(self, name: str, color: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.color = color
        self.setCheckable(True)
        self.setToolTip(name)
        self.setAccessibleName(name)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        size = tokens.WatermarkMetric.SWATCH
        self.setFixedSize(size, size)

    def set_color(self, color: str) -> None:
        self.color = color
        self.setToolTip(color)
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802 - Qt override
        mark = tokens.WatermarkMetric
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        outer = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        inset = mark.SWATCH_RING + 2 if self.isChecked() else 1
        inner = outer.adjusted(inset, inset, -inset, -inset)
        if self.isChecked():
            painter.setPen(QPen(QColor(tokens.BarColor.SWATCH_RING), mark.SWATCH_RING))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            half = mark.SWATCH_RING / 2
            painter.drawRoundedRect(
                outer.adjusted(half, half, -half, -half), mark.SWATCH_RADIUS, mark.SWATCH_RADIUS
            )
        border = design.bar_color("SWATCH_BORDER")
        painter.setPen(QPen(border, 1))
        painter.setBrush(QColor(self.color))
        radius = max(2.0, mark.SWATCH_RADIUS - inset + 1)
        painter.drawRoundedRect(inner, radius, radius)
        painter.end()


class _FontCombo(QComboBox):
    """A combo box styled like the Settings fields. Once a stylesheet
    touches a combo box Qt stops drawing its arrow, so this paints the
    app's own chevron where the drop-down sits."""

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt override
        super().paintEvent(event)
        size = tokens.WatermarkMetric.FONT_COMBO_CHEVRON
        width = tokens.WatermarkMetric.FONT_COMBO_ARROW_W
        pixmap = design.icon("chevron", tokens.Win.TEXT_MUTED).pixmap(size, size)
        painter = QPainter(self)
        painter.drawPixmap(
            self.width() - width + (width - size) // 2 - 4,
            (self.height() - size) // 2,
            pixmap,
        )
        painter.end()


def _field_style() -> str:
    """The Settings text field, as a stylesheet.

    A module function rather than a method of the window, because
    `EntryList`'s add field has to look like the folder and filename fields
    around it and has no window to ask.
    """
    win, metric = tokens.Win, tokens.WinMetric
    return (
        f"QLineEdit {{ background: {win.FIELD_BG};"
        f" border: 1px solid {win.FIELD_BORDER};"
        f" border-radius: {metric.CONTROL_RADIUS}px;"
        f" color: {win.TEXT_PRIMARY}; padding: 0 11px; }}"
    )


class _EntryText(QLabel):
    """An entry as its row shows it: elided, with the whole of it in a
    tooltip when it does not fit.

    Elided rather than left to size itself: a label as wide as a long
    pattern widens the pane, the pane never scrolls sideways, and the row's
    remove control is what would end up out of sight.
    """

    def __init__(self, text: str, parent: QWidget | None = None):
        super().__init__(parent)
        self._full = ""
        # Plain, always: an entry is whatever the user typed, and `<b>` in
        # a hide list is text to hide, not markup to render.
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setFont(_mono_font(12))
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.set_full_text(text)

    def full_text(self) -> str:
        return self._full

    def set_full_text(self, text: str) -> None:
        self._full = text
        self._elide()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._elide()

    def _elide(self) -> None:
        shown = self.fontMetrics().elidedText(
            self._full, Qt.TextElideMode.ElideRight, self.width()
        )
        self.setText(shown)
        self.setToolTip("" if shown == self._full else self._full)


class _RowEditor(QLineEdit):
    """The field an `_EntryRow` becomes while it is being edited.

    Enter, Escape and focus leaving are all caught here rather than read off
    `editingFinished`: that has no notion of abandoning an edit, and on
    focus loss it only fires if the text changed -- so a row clicked and
    then left untouched would stay open as a field.
    """

    committed = pyqtSignal()
    abandoned = pyqtSignal()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.abandoned.emit()
            event.accept()
            return
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            # Accepted rather than passed up: Enter here finishes this row
            # and nothing else in the window.
            self.committed.emit()
            event.accept()
            return
        super().keyPressEvent(event)

    def focusOutEvent(self, event) -> None:
        super().focusOutEvent(event)
        self.committed.emit()


class _EntryRow(QWidget):
    """One entry in an `EntryList`: its text, which becomes a field when
    clicked, its remove control, and room beneath for why it is wrong.

    The row judges nothing. It reports an edit or a removal and the list
    decides, because only the list can see whether new text duplicates
    another row.
    """

    edit_committed = pyqtSignal(str)
    remove_requested = pyqtSignal()

    def __init__(self, entry: str, parent: QWidget | None = None):
        super().__init__(parent)
        win, metric = tokens.Win, tokens.WinMetric
        self._entry = entry
        self._editing = False
        self._problem = ""

        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(metric.ENTRY_REASON_GAP)

        # Named so its stylesheet can say `#entryRow`: QLabel is a QFrame
        # too, and an unqualified QFrame rule would draw a second border
        # around the text inside it.
        self._frame = QFrame()
        self._frame.setObjectName("entryRow")
        self._frame.setFixedHeight(metric.ENTRY_ROW_H)
        self._frame.setCursor(Qt.CursorShape.IBeamCursor)
        # A plain frame is not sent hover events unless it asks, and without
        # them its stylesheet's :hover never applies.
        self._frame.setAttribute(Qt.WidgetAttribute.WA_Hover)
        line = QHBoxLayout(self._frame)
        line.setContentsMargins(metric.ENTRY_TEXT_INSET, 0, metric.ENTRY_REMOVE_INSET, 0)
        line.setSpacing(metric.ENTRY_GAP)

        self._text = _EntryText(entry)
        line.addWidget(self._text, 1)

        self._editor = _RowEditor()
        self._editor.setFont(_mono_font(12))
        self._editor.setStyleSheet(
            "QLineEdit { background: transparent; border: none; padding: 0;"
            f" color: {win.TEXT_PRIMARY}; }}"
        )
        self._editor.committed.connect(lambda: self._finish(commit=True))
        self._editor.abandoned.connect(lambda: self._finish(commit=False))
        self._editor.hide()
        line.addWidget(self._editor, 1)

        self._remove = QPushButton()
        self._remove.setCursor(Qt.CursorShape.PointingHandCursor)
        self._remove.setToolTip("Remove")
        self._remove.setIcon(design.icon("close", win.ICON_IDLE))
        self._remove.setIconSize(QSize(metric.ENTRY_REMOVE_ICON, metric.ENTRY_REMOVE_ICON))
        self._remove.setFixedSize(metric.ENTRY_REMOVE, metric.ENTRY_REMOVE)
        self._remove.setStyleSheet(
            "QPushButton { background: transparent; border: none;"
            f" border-radius: {metric.ENTRY_REMOVE_RADIUS}px; }}"
            f"QPushButton:hover {{ background: {win.CONTROL_BG_HOVER}; }}"
        )
        self._remove.clicked.connect(self.remove_requested)
        line.addWidget(self._remove)
        column.addWidget(self._frame)

        self._reason = QLabel()
        self._reason.setTextFormat(Qt.TextFormat.PlainText)
        self._reason.setWordWrap(True)
        self._reason.setFont(_ui_font(11.5, 400))
        self._reason.setStyleSheet(f"color: {win.ERR_FG};")
        self._reason.hide()
        column.addWidget(self._reason)

        self._refresh()

    def entry(self) -> str:
        return self._entry

    def set_entry(self, entry: str) -> None:
        self._entry = entry
        self._text.set_full_text(entry)

    def is_editing(self) -> bool:
        return self._editing

    def set_problem(self, reason: str | None) -> None:
        self._problem = reason or ""
        self._reason.setText(self._problem)
        self._reason.setVisible(bool(self._problem))
        self._refresh()

    def begin_edit(self) -> None:
        if self._editing:
            return
        self._editing = True
        self._editor.setText(self._entry)
        self._editor.selectAll()
        self._text.hide()
        self._editor.show()
        self._editor.setFocus(Qt.FocusReason.MouseFocusReason)
        self._refresh()

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.begin_edit()
            event.accept()
            return
        super().mousePressEvent(event)

    def _finish(self, commit: bool) -> None:
        if not self._editing:
            return
        # Cleared before the editor is hidden: hiding a focused field moves
        # focus away from it, and the focus-out that follows must find the
        # edit already over rather than commit it a second time -- or commit
        # one that Escape just abandoned.
        self._editing = False
        text = self._editor.text()
        self._editor.hide()
        self._text.show()
        self._refresh()
        if commit:
            self.edit_committed.emit(text)

    def _refresh(self) -> None:
        win, metric = tokens.Win, tokens.WinMetric
        if self._problem:
            border, fill, colour = _rgba("ERR_BORDER"), _rgba("ERR_BG"), win.ERR_FG
        else:
            border, fill, colour = win.FIELD_BORDER, win.FIELD_BG, win.TEXT_PRIMARY
        if self._editing:
            border = tokens.Color.ACCENT
        hover = (
            ""
            if self._editing or self._problem
            else f"#entryRow:hover {{ border-color: {win.CONTROL_BORDER_HOVER}; }}"
        )
        self._frame.setStyleSheet(
            f"#entryRow {{ background: {fill}; border: 1px solid {border};"
            f" border-radius: {metric.ENTRY_ROW_RADIUS}px; }}" + hover
        )
        self._text.setStyleSheet(f"color: {colour}; background: transparent;")


class EntryList(QWidget):
    """A list of short strings edited as rows: one row per entry, each with
    its own remove control, and a field beneath for adding the next.

    Rows rather than lines in a textarea because an entry has its own
    mistakes -- a row can be told it is wrong, where a line in a box can
    only be listed somewhere underneath it.

    Entries are always trimmed, never blank and never repeated. The add
    field refuses a blank or a duplicate as it is typed, rather than
    accepting it here for a save to drop silently later.
    """

    changed = pyqtSignal()

    def __init__(self, placeholder: str, parent: QWidget | None = None):
        super().__init__(parent)
        win, metric = tokens.Win, tokens.WinMetric
        self._rows: list[_EntryRow] = []
        self._problems: dict[str, str] = {}

        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(metric.ENTRY_ROW_GAP)

        self._row_column = QVBoxLayout()
        self._row_column.setContentsMargins(0, 0, 0, 0)
        self._row_column.setSpacing(metric.ENTRY_ROW_GAP)
        column.addLayout(self._row_column)

        add_row = QHBoxLayout()
        add_row.setContentsMargins(0, 0, 0, 0)
        add_row.setSpacing(metric.ENTRY_GAP)
        self._add_field = QLineEdit()
        self._add_field.setPlaceholderText(placeholder)
        self._add_field.setFont(_mono_font(12))
        self._add_field.setFixedHeight(metric.CONTROL_H)
        self._add_field.setStyleSheet(_field_style())
        self._add_field.textChanged.connect(lambda _text: self._refresh_add())
        self._add_field.returnPressed.connect(self._add_typed)
        add_row.addWidget(self._add_field, 1)
        self._add_button = SecondaryButton("Add")
        self._add_button.clicked.connect(self._add_typed)
        add_row.addWidget(self._add_button)
        column.addLayout(add_row)

        self._add_note = QLabel("Already in the list.")
        self._add_note.setFont(_ui_font(11.5, 400))
        self._add_note.setStyleSheet(f"color: {win.WARN_FG};")
        self._add_note.hide()
        column.addWidget(self._add_note)

        self._refresh_add()

    def entries(self) -> list[str]:
        return [row.entry() for row in self._rows]

    def set_entries(self, entries: list[str]) -> None:
        """Replace every entry without emitting `changed`: seeding from what
        is stored is not an edit, the same split every other control in
        Settings keeps."""
        # Emptied before the old rows go: a row still open for editing
        # commits as it is hidden, and that commit must find nothing to
        # change rather than report an edit to a list being replaced.
        old, self._rows = self._rows, []
        for row in old:
            self._discard(row)
        for entry in entries:
            entry = entry.strip()
            if entry and entry not in self.entries():
                self._append(entry)
        self._refresh_add()

    def set_problems(self, problems: dict[str, str]) -> None:
        """Show `{entry: reason}` beneath the rows it names. Kept, not just
        applied, so a row added or edited into a named entry shows it too;
        an empty dict clears every row."""
        self._problems = dict(problems)
        for row in self._rows:
            row.set_problem(self._problems.get(row.entry()))

    def _append(self, entry: str) -> None:
        row = _EntryRow(entry)
        row.edit_committed.connect(lambda text, r=row: self._commit_edit(r, text))
        row.remove_requested.connect(lambda r=row: self._remove(r))
        row.set_problem(self._problems.get(entry))
        self._rows.append(row)
        self._row_column.addWidget(row)

    def _discard(self, row: _EntryRow) -> None:
        self._row_column.removeWidget(row)
        row.hide()
        row.deleteLater()

    def _add_typed(self) -> None:
        entry = self._add_field.text().strip()
        if not entry or entry in self.entries():
            # Left in the field, not cleared: a duplicate is most likely a
            # near-miss the user will want to correct rather than retype.
            return
        self._append(entry)
        self._add_field.clear()
        self.changed.emit()

    def _commit_edit(self, row: _EntryRow, text: str) -> None:
        if row not in self._rows:
            return
        entry = text.strip()
        others = [r.entry() for r in self._rows if r is not row]
        if not entry or entry == row.entry() or entry in others:
            # Abandoned rather than applied. Clearing a row is not how an
            # entry is removed -- that is what its remove control is for --
            # and two rows with the same text could not be told apart by
            # `set_problems`.
            return
        row.set_entry(entry)
        row.set_problem(self._problems.get(entry))
        self._refresh_add()
        self.changed.emit()

    def _remove(self, row: _EntryRow) -> None:
        if row not in self._rows:
            return
        self._rows.remove(row)
        self._discard(row)
        self._refresh_add()
        self.changed.emit()

    def _refresh_add(self) -> None:
        entry = self._add_field.text().strip()
        duplicate = bool(entry) and entry in self.entries()
        self._add_button.setEnabled(bool(entry) and not duplicate)
        self._add_note.setVisible(duplicate)


class _HideListSection(QWidget):
    """One kind of entry on the Hide sensitive page: its title, what it
    does, and its entries as an `EntryList` -- or, on request, as text.

    Text stays for the one job rows are worse at: pasting many entries at
    once, or copying the lot to another machine. Both show the same
    entries, so swapping between them is not an edit; typing in the text
    is.
    """

    changed = pyqtSignal()

    def __init__(
        self,
        title: str,
        note: str,
        placeholder: str,
        entries: list[str],
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        win, metric = tokens.Win, tokens.WinMetric
        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(metric.FIELD_GAP)

        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(metric.ENTRY_GAP)
        heading = QLabel(title)
        heading.setFont(_ui_font(12.5, 600))
        heading.setStyleSheet(f"color: {win.TEXT_PRIMARY};")
        top.addWidget(heading, 1)
        self.as_text = SecondaryButton("Edit as text")
        self.as_text.setCheckable(True)
        self.as_text.setFont(_ui_font(11, 500))
        self.as_text.setFixedHeight(metric.HIDE_TOGGLE_H)
        self.as_text.toggled.connect(self._show_as_text)
        top.addWidget(self.as_text)
        column.addLayout(top)

        caption = QLabel(note)
        caption.setWordWrap(True)
        caption.setFont(_ui_font(11.5, 400))
        caption.setStyleSheet(f"color: {win.TEXT_FAINT};")
        column.addWidget(caption)

        self.rows = EntryList(placeholder)
        self.rows.set_entries(entries)
        self.rows.changed.connect(self.changed)
        column.addWidget(self.rows)

        self.text = QPlainTextEdit()
        self.text.setFont(_mono_font(12))
        self.text.setFixedHeight(metric.HIDE_TEXT_H)
        self.text.setStyleSheet(
            _field_style().replace("QLineEdit", "QPlainTextEdit") + scrollbar_style()
        )
        self.text.textChanged.connect(self.changed)
        self.text.hide()
        column.addWidget(self.text)

        # The rows carry their own complaints. This is only for while the
        # entries are text, when there is no row to put one on.
        self.complaints = QLabel()
        self.complaints.setTextFormat(Qt.TextFormat.PlainText)
        self.complaints.setWordWrap(True)
        self.complaints.setFont(_ui_font(11.5, 400))
        self.complaints.setStyleSheet(f"color: {win.ERR_FG};")
        self.complaints.hide()
        column.addWidget(self.complaints)

    def is_text(self) -> bool:
        return self.as_text.isChecked()

    def entries(self) -> list[str]:
        """The entries as they stand -- read from the text while it is
        showing, so Save writes what was pasted without anyone having to
        swap back first."""
        if self.is_text():
            return self.text.toPlainText().splitlines()
        return self.rows.entries()

    def set_complaints(self, by_entry: dict[str, str]) -> None:
        self.rows.set_problems(by_entry)
        self.complaints.setText("\n".join(by_entry.values()))
        self.complaints.setVisible(self.is_text() and bool(by_entry))

    def _show_as_text(self, as_text: bool) -> None:
        if as_text:
            # Signals held while it is filled: the same entries appearing in
            # the box is not the user typing, and must not mark Settings
            # dirty.
            self.text.blockSignals(True)
            self.text.setPlainText("\n".join(self.rows.entries()))
            self.text.blockSignals(False)
        else:
            self.rows.set_entries(self.text.toPlainText().splitlines())
        self.rows.setVisible(not as_text)
        self.text.setVisible(as_text)
        self.complaints.setVisible(as_text and bool(self.complaints.text()))
        self.as_text.setText("Edit as list" if as_text else "Edit as text")


class _NavRow(QPushButton):
    def __init__(self, icon_name: str, label: str, parent=None):
        super().__init__(label, parent)
        metric = tokens.WinMetric
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFont(_ui_font(12.5, 500))
        self.setIcon(design.icon(icon_name, tokens.Win.ICON_IDLE))
        self.setIconSize(QSize(metric.NAV_ICON, metric.NAV_ICON))
        self._icon_name = icon_name
        self.toggled.connect(self._refresh)
        self._refresh(False)

    def _refresh(self, checked: bool) -> None:
        metric, win = tokens.WinMetric, tokens.Win
        self.setIcon(
            design.icon(self._icon_name, win.ICON_ACTIVE if checked else win.ICON_IDLE)
        )
        background = win.SELECTED_BG if checked else "transparent"
        colour = win.ICON_ACTIVE if checked else win.TEXT_MUTED
        self.setStyleSheet(
            f"QPushButton {{ text-align: left; background: {background};"
            f" color: {colour}; border: none;"
            f" border-radius: {metric.NAV_ROW_RADIUS}px;"
            f" padding: {metric.NAV_ROW_PAD[0]}px {metric.NAV_ROW_PAD[1]}px; }}"
            + (
                ""
                if checked
                else f"QPushButton:hover {{ background: {win.ROW_HOVER}; }}"
            )
        )


def _pane(*widgets: QWidget) -> QWidget:
    """A content pane: padded, top-aligned, scrollable by its caller."""
    metric = tokens.WinMetric
    page = QWidget()
    column = QVBoxLayout(page)
    column.setContentsMargins(
        metric.PANE_PAD[1], metric.PANE_PAD[0], metric.PANE_PAD[1], metric.PANE_PAD[0]
    )
    column.setSpacing(metric.FIELD_GAP)
    for widget in widgets:
        if widget is None:
            column.addSpacing(metric.GROUP_GAP - metric.FIELD_GAP)
        else:
            column.addWidget(widget)
    column.addStretch()
    return page


class SettingsWindow(WinWindow):
    """Settings. `on_saved` fires once Save has committed, so the controller
    that owns the tray can rebind the shortcut without this window knowing
    what a controller is.
    """

    def __init__(
        self,
        *,
        parent: QWidget | None = None,
        config_dir: Path | None = None,
        on_saved: Callable[[], None] | None = None,
    ):
        super().__init__(
            "Snipux Settings",
            size=(tokens.WinMetric.SETTINGS_W, tokens.WinMetric.SETTINGS_H),
            parent=parent,
        )
        self._config_dir = config_dir
        self._on_saved = on_saved
        self._dirty = False

        body = QHBoxLayout(self.body)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        body.addWidget(self._build_nav())
        body.addWidget(self._build_panes(), 1)

        self._build_footer_contents()
        self._nav_group.buttons()[0].setChecked(True)
        self._refresh_dirty()

    # -- structure -------------------------------------------------------

    def _build_nav(self) -> QWidget:
        metric, win = tokens.WinMetric, tokens.Win
        rail = QWidget()
        rail.setFixedWidth(metric.NAV_W)
        # Scoped to the rail itself. Unscoped, the border rule reached every
        # child without a sheet of its own, and the version label drew a
        # hairline down its own right edge, inside the rail.
        rail.setObjectName("navRail")
        rail.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        rail.setStyleSheet(
            f"QWidget#navRail {{ background: {win.CHROME_BG};"
            f" border-right: 1px solid {win.SEPARATOR}; }}"
        )
        column = QVBoxLayout(rail)
        column.setContentsMargins(
            metric.NAV_PAD[1], metric.NAV_PAD[0], metric.NAV_PAD[1], metric.NAV_PAD[0]
        )
        column.setSpacing(metric.NAV_ROW_GAP)

        self._nav_group = QButtonGroup(self)
        self._nav_group.setExclusive(True)
        for index, (_id, icon_name, label) in enumerate(tokens.SETTINGS_NAV):
            # "&" in a button label is Qt's mnemonic marker: "Tray & startup"
            # renders as "Tray _startup" unless it is doubled.
            row = _NavRow(icon_name, label.replace("&", "&&"))
            self._nav_group.addButton(row, index)
            column.addWidget(row)
        self._nav_group.idClicked.connect(self._show_pane)

        column.addStretch()
        self._version_label = QLabel(setup_desktop.version_line())
        self._version_label.setFont(_ui_font(11, 400))
        self._version_label.setStyleSheet(f"color: {tokens.Win.TEXT_DISABLED};")
        # The rail's width is fixed (NAV_W); a version/Qt/session-type line
        # can run longer than that on some platforms, and this label has no
        # room to grow into. Wrap rather than let Qt silently clip it to a
        # single line -- the "unknow" truncation this exists to fix.
        self._version_label.setWordWrap(True)
        column.addWidget(self._version_label)
        return rail

    def _build_panes(self) -> QWidget:
        self._panes = QStackedWidget()
        self._panes.setStyleSheet("background: transparent;")
        # Built in the rail's own order and looked up by id, so a page added
        # to `tokens.SETTINGS_NAV` cannot open under another page's row.
        builders = {
            "capture": self._capture_pane,
            "saving": self._saving_pane,
            "ink": self._annotation_pane,
            "watermark": self._watermark_pane,
            "hide": self._hide_pane,
            "tray": self._tray_pane,
        }
        for identifier, _icon, _label in tokens.SETTINGS_NAV:
            build = builders[identifier]
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QFrame.Shape.NoFrame)
            # Panes wrap; they never scroll sideways. A horizontal scrollbar
            # here hides the right-hand control of every row behind it.
            scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            # Scoped to the scroll area itself: an unqualified rule here
            # would also claim its scrollbar, and beat the window's own
            # sheet to it.
            scroll.setStyleSheet("QScrollArea { background: transparent; }" + scrollbar_style())
            scroll.setWidget(build())
            self._panes.addWidget(scroll)
        return self._panes

    def _show_pane(self, index: int) -> None:
        self._panes.setCurrentIndex(index)

    # -- panes -----------------------------------------------------------

    def _capture_pane(self) -> QWidget:
        self._recorder = ShortcutRecorder(setup_desktop.load_shortcut(self._config_dir))
        self._recorder.recorded.connect(self._on_recorded)

        self._conflict = ConflictBanner()
        self._conflict.show_for(self._recorder.shortcut_value())

        if HotkeyEventFilter.is_available():
            why_text = (
                "Windows keeps no registry of who owns a shortcut the way "
                "GNOME does, so this checks the one thing that is always "
                "already taken — the Windows Snipping Tool's own "
                "Win+Shift+S — and, for everything else, whether Windows "
                "itself refuses to register the combination."
            )
        else:
            why_text = (
                "GNOME accepts two applications claiming the same combination "
                "without a word, then fires whichever it likes. This check is the "
                "only warning you get — and it cannot see applications that "
                "grab a key directly rather than through GNOME."
            )
        why = QLabel(why_text)
        why.setWordWrap(True)
        why.setFont(_ui_font(11.5, 400))
        why.setStyleSheet(f"color: {tokens.Win.TEXT_FAINT};")

        self._after_group = QButtonGroup(self)
        self._after_group.setExclusive(True)
        cards = []
        stored = setup_desktop.load_after_capture(self._config_dir)
        for index, (identifier, label, note) in enumerate(tokens.AFTER_CAPTURE):
            card = RadioCard(label, note)
            card.setChecked(identifier == stored)
            card.toggled.connect(lambda _c: self._mark_dirty())
            self._after_group.addButton(card, index)
            cards.append(card)

        self._instant_saves = SwitchRow(
            "Save instead of copying",
            "Only changes Capture and finish above -- on writes the file "
            "and skips the clipboard; off copies, same as today.",
            setup_desktop.load_instant_saves(self._config_dir),
        )
        self._instant_saves.switch.toggled.connect(lambda _c: self._mark_dirty())

        # "Start on the last region" deliberately has no row here. It lived
        # in this pane first and went unnoticed until it was pointed out --
        # a preference nobody finds is a preference nobody has -- so it is
        # the Last region row of the chooser's mode menu now, and choosing
        # it is what the next snip opens on. See `Chooser.set_mode`.

        return _pane(
            SectionHeading("Shortcut"),
            self._recorder,
            self._conflict,
            why,
            None,
            SectionHeading("After capture"),
            *cards,
            self._instant_saves,
        )

    def _hide_pane(self) -> QWidget:
        """The user's own hide list: a list of rows for each kind of entry.

        Edited here rather than in a text editor because the file is no use
        if nobody can find it -- and because an entry that would black out
        half the screen is worth catching while the user is looking at it,
        not on the next capture. Rows rather than a box per kind, so that
        catch lands on the entry it is about.
        """
        stored = setup_desktop.load_hide_list(self._config_dir)
        intro = QLabel(
            "Snipux already finds cards, keys, emails and the values next to "
            "labels like Password. Add the things only you know are "
            "sensitive."
        )
        intro.setWordWrap(True)
        intro.setFont(_ui_font(11.5, 400))
        intro.setStyleSheet(f"color: {tokens.Win.TEXT_FAINT};")

        placeholders = {
            "words": "Add a word or phrase",
            "labels": "Add a field name",
            "patterns": "Add a pattern",
        }
        rows: list[QWidget | None] = []
        self._hide_lists: dict[str, _HideListSection] = {}
        for section, title, note in tokens.HIDE_LIST_FIELDS:
            hide_list = _HideListSection(
                title, note, placeholders[section], stored.get(section, [])
            )
            hide_list.changed.connect(self._on_hide_list_edited)
            self._hide_lists[section] = hide_list
            rows += [hide_list, None]

        self._hide_file = QLineEdit(str(setup_desktop.hide_list_path(self._config_dir)))
        self._hide_file.setReadOnly(True)
        self._hide_file.setFont(_mono_font(12))
        self._hide_file.setFixedHeight(tokens.WinMetric.CONTROL_H)
        self._hide_file.setStyleSheet(_field_style())
        where = QLabel(
            "The same list as a plain text file -- edit it here, or by hand, "
            "or copy it to another machine."
        )
        where.setWordWrap(True)
        where.setFont(_ui_font(11.5, 400))
        where.setStyleSheet(f"color: {tokens.Win.TEXT_FAINT};")

        # Checked before anything is edited: a hand-edited file can already
        # hold an entry Save will refuse, and its row should say so now
        # rather than when Save does.
        self._refresh_hide_complaints()
        return _pane(
            SectionHeading("Your own list"),
            intro,
            None,
            *rows,
            SectionHeading("Where it is kept"),
            where,
            self._hide_file,
        )

    def _on_hide_list_edited(self) -> None:
        self._mark_dirty()
        self._refresh_hide_complaints()

    def _hide_list_entries(self) -> dict[str, list[str]]:
        return {
            section: self._hide_lists[section].entries()
            for section, _title, _note in tokens.HIDE_LIST_FIELDS
        }

    def _hide_list_complaints(self) -> list[str]:
        entries = self._hide_list_entries()
        return setup_desktop.validate_hide_list(
            entries["words"], entries["labels"], entries["patterns"]
        )

    def _refresh_hide_complaints(self) -> None:
        for section, hide_list in self._hide_lists.items():
            hide_list.set_complaints(
                self._hide_list_complaints_by_entry(section, hide_list.entries())
            )

    @staticmethod
    def _hide_list_complaints_by_entry(section: str, entries: list[str]) -> dict[str, str]:
        """`validate_hide_list`'s complaints about one section's entries,
        keyed by the entry each is about.

        Each entry is validated on its own, rather than the whole list at
        once with each complaint matched back to an entry by the quote in
        it. A complaint is about a single line either way, and this way no
        entry's text can be mistaken for the quote of another.
        """
        by_entry: dict[str, str] = {}
        for line in entries:
            entry = line.strip()
            if not entry or entry in by_entry:
                continue
            lists: dict[str, list[str]] = {name: [] for name in setup_desktop.HIDE_LIST_SECTIONS}
            lists[section] = [entry]
            complaints = setup_desktop.validate_hide_list(
                lists["words"], lists["labels"], lists["patterns"]
            )
            if complaints:
                by_entry[entry] = "\n".join(complaints)
        return by_entry

    def _saving_pane(self) -> QWidget:
        folder_row = QHBoxLayout()
        self._folder = QLineEdit(str(setup_desktop.load_save_folder(self._config_dir)))
        self._folder.setReadOnly(True)
        self._folder.setFont(_mono_font(12))
        self._folder.setFixedHeight(tokens.WinMetric.CONTROL_H)
        self._folder.setStyleSheet(_field_style())
        choose = SecondaryButton("Choose…")
        choose.clicked.connect(self._choose_folder)
        folder_row.addWidget(self._folder, 1)
        folder_row.addWidget(choose)
        folder_widget = QWidget()
        folder_widget.setLayout(folder_row)
        folder_row.setContentsMargins(0, 0, 0, 0)

        self._filename = QLineEdit(setup_desktop.load_filename_pattern(self._config_dir))
        self._filename.setFont(_mono_font(12))
        self._filename.setFixedHeight(tokens.WinMetric.CONTROL_H)
        self._filename.setStyleSheet(_field_style())
        self._filename.textChanged.connect(self._refresh_preview)

        self._preview = QLabel()
        self._preview.setFont(_mono_font(11.5))
        self._preview.setStyleSheet(f"color: {tokens.Win.PATH_FG};")
        self._preview.setWordWrap(True)

        # A grid, not a row: eight chips in one line are wider than the pane,
        # and a pane that scrolls sideways puts the controls beside them --
        # Choose..., the switch -- off the edge of the window.
        chips = QGridLayout()
        chips.setSpacing(6)
        chips.setContentsMargins(0, 0, 0, 0)
        for index, (token, label) in enumerate(tokens.FILENAME_TOKENS):
            chip = SecondaryButton(f"{token}  {label}")
            chip.setFixedHeight(26)
            chip.setFont(_ui_font(11, 500))
            chip.clicked.connect(lambda _c, t=token: self._append_token(t))
            chips.addWidget(chip, index // 4, index % 4)
        chip_row = QWidget()
        chip_row.setLayout(chips)

        self._native = SwitchRow(
            "Save at native resolution",
            "On writes 2× pixels on HiDPI; off saves what you saw.",
            setup_desktop.load_native_resolution(self._config_dir),
        )
        self._native.switch.toggled.connect(lambda _c: self._mark_dirty())

        self._frame_rate = SpinRow(
            "Recording frame rate",
            "GNOME recordings ask for this many frames per second. Windows "
            "measures its own rate instead and ignores this.",
            setup_desktop.load_recording_frame_rate(self._config_dir),
        )
        self._frame_rate.spin.valueChanged.connect(lambda _v: self._mark_dirty())

        # The same flag the chooser row now carries, reading and writing the
        # same stored value -- so the two can never disagree. Greyed here
        # where the platform cannot honour it, rather than left as a switch
        # that moves and changes nothing, which is what this was on Windows.
        records_cursor = platform.current.records_cursor()
        self._draw_cursor = SwitchRow(
            "Show the cursor in recordings",
            "Composites the mouse pointer into the video."
            if records_cursor
            else platform.current.cursor_toggle_unavailable_reason(),
            setup_desktop.load_recording_draw_cursor(self._config_dir),
        )
        self._draw_cursor.switch.setEnabled(records_cursor)
        self._draw_cursor.switch.toggled.connect(lambda _c: self._mark_dirty())

        # Recording's destination, folder and filename. None of these had a
        # Settings surface before: the destination could only be set on the
        # chooser, per-capture, and recordings had no folder or pattern of
        # their own at all -- they borrowed the stills ones above, which is
        # how a video landed in ~/Pictures/snipux called "Screenshot from
        # ....mp4".
        self._recording_after_group = QButtonGroup(self)
        self._recording_after_group.setExclusive(True)
        recording_cards = []
        stored_after = setup_desktop.load_recording_after(self._config_dir)
        # Qt has no GIF encoder (`QImageWriter` carries no such plugin), so
        # landing one needs the system ffmpeg the player's own export row
        # already gates on -- greyed here the same way, with the same
        # wording, rather than a second check invented for this pane.
        gif_reason = player.export_availability(False).get("gif", "")
        if stored_after == "gif" and gif_reason:
            # Stored on a machine that had ffmpeg, opened on one that does
            # not: nothing here re-checks that box, so falling back to the
            # ordinary default is what keeps `checkedId()` from landing on
            # -1 -- every card unchecked -- the moment this pane is saved.
            stored_after = tokens.RECORD_AFTER_DEFAULT
        for index, (identifier, label, note) in enumerate(tokens.RECORDING_AFTER):
            card = RadioCard(label, note)
            if identifier == "gif" and gif_reason:
                card.set_unavailable(gif_reason)
            else:
                card.setChecked(identifier == stored_after)
            card.toggled.connect(lambda _c: self._mark_dirty())
            self._recording_after_group.addButton(card, index)
            recording_cards.append(card)

        recording_folder_row = QHBoxLayout()
        self._recording_folder = QLineEdit(
            str(setup_desktop.load_recording_folder(self._config_dir))
        )
        self._recording_folder.setReadOnly(True)
        self._recording_folder.setFont(_mono_font(12))
        self._recording_folder.setFixedHeight(tokens.WinMetric.CONTROL_H)
        self._recording_folder.setStyleSheet(_field_style())
        choose_recording = SecondaryButton("Choose…")
        choose_recording.clicked.connect(self._choose_recording_folder)
        recording_folder_row.addWidget(self._recording_folder, 1)
        recording_folder_row.addWidget(choose_recording)
        recording_folder_widget = QWidget()
        recording_folder_widget.setLayout(recording_folder_row)
        recording_folder_row.setContentsMargins(0, 0, 0, 0)

        self._recording_filename = QLineEdit(
            setup_desktop.load_recording_filename_pattern(self._config_dir)
        )
        self._recording_filename.setFont(_mono_font(12))
        self._recording_filename.setFixedHeight(tokens.WinMetric.CONTROL_H)
        self._recording_filename.setStyleSheet(_field_style())
        self._recording_filename.textChanged.connect(self._refresh_recording_preview)

        self._recording_preview = QLabel()
        self._recording_preview.setFont(_mono_font(11.5))
        self._recording_preview.setStyleSheet(f"color: {tokens.Win.PATH_FG};")
        self._recording_preview.setWordWrap(True)

        self._refresh_preview()
        self._refresh_recording_preview()
        return _pane(
            SectionHeading("Folder"),
            folder_widget,
            None,
            SectionHeading("Filename"),
            self._filename,
            self._preview,
            chip_row,
            None,
            self._native,
            SectionHeading("Recording"),
            self._frame_rate,
            self._draw_cursor,
            None,
            SectionHeading("When a recording finishes"),
            *recording_cards,
            None,
            SectionHeading("Recordings folder"),
            recording_folder_widget,
            self._recording_filename,
            self._recording_preview,
        )

    def _annotation_pane(self) -> QWidget:
        win = tokens.Win
        note = QLabel(
            "These set the state the overlay opens in. The tools themselves "
            "are the overlay's own."
        )
        note.setWordWrap(True)
        note.setFont(_ui_font(11.5, 400))
        note.setStyleSheet(f"color: {win.TEXT_NOTE};")

        self._opening_tool = Select(
            [
                (tool, "No tool", "select") if tool == tokens.OPENING_TOOL_NONE
                else (tool, tokens.TOOL_NAMES[tool], tokens.TOOL_GLYPHS.get(tool, tool))
                for tool in tokens.OPENING_TOOLS
            ],
            setup_desktop.load_default_tool(self._config_dir),
        )
        self._opening_tool.changed.connect(lambda _t: self._mark_dirty())
        opening_row = _ControlRow("Tool the overlay opens with", "", self._opening_tool)

        self._remember_tool = SwitchRow(
            "Remember my last tool instead",
            "Opens with whichever tool your last snip ended on. The tool "
            "above is used until there is one.",
            setup_desktop.load_remember_tool(self._config_dir),
        )
        self._remember_tool.switch.toggled.connect(lambda _c: self._mark_dirty())

        self._default_ink = ColourPicker(
            setup_desktop.load_default_ink(self._config_dir),
            "Each tool's own",
            _EACH_TOOLS_OWN,
        )
        self._default_ink.changed.connect(self._on_default_ink_changed)
        ink_row = _ControlRow(
            "Default ink colour",
            "Every tool that draws in a colour starts with this.",
            self._default_ink,
        )

        self._show_hints = SwitchRow(
            "Show the hint bar",
            "Esc discard ink · Enter copy & close, across the top of the "
            "overlay. Off by default; press ? in the overlay to reveal it "
            "for one session without changing this.",
            setup_desktop.load_hints_enabled(self._config_dir),
        )
        self._show_hints.switch.toggled.connect(lambda _c: self._mark_dirty())

        # Per-tool defaults: one tool at a time, picked from a dropdown, with
        # only the rows its style popover has -- thirteen tools' worth of
        # controls at once would be most of a screen of Settings.
        self._tool_edits = setup_desktop.load_tool_defaults(self._config_dir)
        styled = [tool for tool in tokens.TOOLS if tokens.STYLE_SECTIONS.get(tool)]
        self._custom_tool = Select(
            [(tool, tokens.TOOL_NAMES[tool], tokens.TOOL_GLYPHS.get(tool, tool)) for tool in styled],
            styled[0],
        )
        self._custom_tool.changed.connect(lambda _t: self._load_tool_editor())

        self._tool_colour = ColourPicker(None, "Default", [])
        self._tool_colour.changed.connect(lambda colour: self._edit_tool("color", colour))
        self._tool_size = SliderControl(5, tokens.STROKE_RANGE, "px")
        self._tool_size.changed.connect(lambda value: self._edit_tool("size", value))
        self._tool_dash = Select(
            [(value, label, None) for value, _pattern, label in tokens.DASH_CYCLE], "solid"
        )
        self._tool_dash.changed.connect(lambda value: self._edit_tool("dash", value))
        self._tool_fill = Select(
            [(value, label, None) for value, label in tokens.FILL_CYCLE], "outline"
        )
        self._tool_fill.changed.connect(lambda value: self._edit_tool("fill", value))
        self._tool_strength = SliderControl(tokens.Metric.BLUR_DEFAULT, tokens.STRENGTH_RANGE)
        self._tool_strength.changed.connect(lambda value: self._edit_tool("strength", value))
        self._tool_snap = Select(
            [(value, label, None) for value, label in tokens.SNAP_CYCLE], "text"
        )
        self._tool_snap.changed.connect(lambda value: self._edit_tool("snap", value))
        # In `tokens.STYLE_SECTIONS`' own names, so a tool's sections are
        # the rows it shows.
        self._tool_rows = {
            "color": _ControlRow("Colour", "", self._tool_colour),
            "size": _ControlRow("Size", "", self._tool_size),
            "dash": _ControlRow("Line", "", self._tool_dash),
            "fill": _ControlRow("Fill", "", self._tool_fill),
            "strength": _ControlRow("Strength", "", self._tool_strength),
            "snap": _ControlRow("Sweep", "", self._tool_snap),
        }

        reset = QPushButton("Reset all tools")
        reset.setCursor(Qt.CursorShape.PointingHandCursor)
        reset.setFont(_ui_font(11.5, 500))
        reset.setStyleSheet(
            f"QPushButton {{ border: none; background: transparent; color: {win.TEXT_MUTED}; }}"
            f"QPushButton:hover {{ color: {win.TEXT_PRIMARY}; }}"
        )
        reset.clicked.connect(lambda _c=False: self._reset_tool_defaults())
        self._reset_tools = reset
        heading = QWidget()
        heading_row = QHBoxLayout(heading)
        heading_row.setContentsMargins(0, 0, 0, 0)
        heading_row.addWidget(SectionHeading("Per-tool defaults"))
        heading_row.addStretch()
        heading_row.addWidget(reset)

        advanced_note = QLabel(
            "What each tool starts a session with. Anything left alone follows "
            "the defaults above."
        )
        advanced_note.setWordWrap(True)
        advanced_note.setFont(_ui_font(11.5, 400))
        advanced_note.setStyleSheet(f"color: {win.TEXT_NOTE};")

        self._load_tool_editor()
        return _pane(
            SectionHeading("Annotation"),
            note,
            None,
            opening_row,
            self._remember_tool,
            ink_row,
            self._show_hints,
            None,
            heading,
            advanced_note,
            _ControlRow("Tool", "", self._custom_tool),
            *self._tool_rows.values(),
        )

    @staticmethod
    def _shipped_style(tool: str) -> dict:
        """What `tool` draws with before anyone sets anything, keyed by
        `tokens.STYLE_SECTIONS`' names."""
        seed = tokens.DEFAULT_STYLE.get(tool, tokens.DEFAULT_STYLE_OTHER)
        return {
            "color": seed["color"],
            "size": seed["size"],
            "dash": seed["dash"],
            "fill": seed["fill"],
            "strength": tokens.Metric.BLUR_DEFAULT,
            "snap": seed.get("snap", "text"),
        }

    def _inherited_colour(self, tool: str) -> str:
        """The colour `tool` gets with none of its own: the default ink, or
        the one it shipped with."""
        return self._default_ink.value() or self._shipped_style(tool)["color"]

    def _load_tool_editor(self) -> None:
        """Show the picked tool's rows, holding its values -- its own where
        it has them, what it shipped with where it does not."""
        tool = self._custom_tool.value()
        sections = tokens.STYLE_SECTIONS.get(tool, [])
        values = {**self._shipped_style(tool), **self._tool_edits.get(tool, {})}
        self._tool_colour.set_inherit("Default", [self._inherited_colour(tool)])
        self._tool_colour.set_value(self._tool_edits.get(tool, {}).get("color"))
        self._tool_size.set_value(values["size"])
        self._tool_dash.set_value(values["dash"])
        self._tool_fill.set_value(values["fill"])
        self._tool_strength.set_value(values["strength"])
        self._tool_snap.set_value(values["snap"])
        for section, row in self._tool_rows.items():
            row.setVisible(section in sections)

    def _edit_tool(self, section: str, value) -> None:
        """Record one of the picked tool's defaults. Only what differs from
        how it shipped is kept -- except a colour, which is kept whenever
        one is chosen: the shipped colour, chosen, is how a tool opts out of
        the default ink."""
        tool = self._custom_tool.value()
        fields = self._tool_edits.setdefault(tool, {})
        shipped = self._shipped_style(tool)[section]
        if value is None or (section != "color" and value == shipped):
            fields.pop(section, None)
        else:
            fields[section] = value
        if not fields:
            del self._tool_edits[tool]
        self._mark_dirty()

    def _on_default_ink_changed(self, _colour) -> None:
        tool = self._custom_tool.value()
        self._tool_colour.set_inherit("Default", [self._inherited_colour(tool)])
        self._mark_dirty()

    def _reset_tool_defaults(self) -> None:
        if not self._tool_edits:
            return
        self._tool_edits = {}
        self._load_tool_editor()
        self._mark_dirty()

    def _watermark_pane(self) -> QWidget:
        """What the stills bar's watermark stamps: a line of text, or an
        image.

        Only what the mark is. Whether it is on, its corner and its opacity
        are the bar's, picked per snip where its preview shows them, so none
        of that is here. Nothing is written before Save, the image included:
        choosing one only shows it, and Save is what copies it in.
        """
        win, metric = tokens.Win, tokens.WinMetric
        mark = tokens.WatermarkMetric

        intro = QLabel(
            "The watermark button on the stills bar stamps this on a snip, in "
            "the corner and at the opacity picked there. It lands once, when "
            "the snip is copied or saved, above everything drawn on it."
        )
        intro.setWordWrap(True)
        intro.setFont(_ui_font(11.5, 400))
        intro.setStyleSheet(f"color: {win.TEXT_FAINT};")

        self._watermark_kind_group = QButtonGroup(self)
        self._watermark_kind_group.setExclusive(True)
        self._watermark_cards: dict[str, RadioCard] = {}
        stored_kind = setup_desktop.load_watermark_kind(self._config_dir)
        for index, (kind, label, note) in enumerate(tokens.WATERMARK_KINDS):
            card = RadioCard(label, note)
            card.setChecked(kind == stored_kind)
            card.toggled.connect(lambda _c: self._mark_dirty())
            self._watermark_kind_group.addButton(card, index)
            self._watermark_cards[kind] = card

        self._watermark_text = QLineEdit(setup_desktop.load_watermark_text(self._config_dir))
        self._watermark_text.setPlaceholderText("Your name, a team, a project")
        self._watermark_text.setMaxLength(mark.TEXT_MAX_CHARS)
        self._watermark_text.setFont(_ui_font(12.5, 400))
        self._watermark_text.setFixedHeight(metric.CONTROL_H)
        self._watermark_text.setStyleSheet(_field_style())
        # `textEdited`, not `textChanged`: only typing is an edit.
        self._watermark_text.textEdited.connect(self._on_watermark_text_edited)
        text_style = self._watermark_text_style_rows()

        self._watermark_thumb = QLabel()
        self._watermark_thumb.setFixedSize(mark.THUMB_W, mark.THUMB_H)
        self._watermark_thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._watermark_thumb.setFont(_ui_font(11.5, 400))
        self._watermark_thumb.setStyleSheet(
            f"QLabel {{ background: {win.FIELD_BG}; border: 1px solid {win.FIELD_BORDER};"
            f" border-radius: {metric.CONTROL_RADIUS}px; color: {win.TEXT_FAINT}; }}"
        )
        choose = SecondaryButton("Choose…")
        choose.clicked.connect(lambda _checked=False: self._choose_watermark_image())
        self._watermark_remove = SecondaryButton("Remove")
        self._watermark_remove.clicked.connect(lambda _checked=False: self._remove_watermark_image())
        buttons = QVBoxLayout()
        buttons.setContentsMargins(0, 0, 0, 0)
        buttons.setSpacing(metric.ENTRY_ROW_GAP)
        buttons.addWidget(choose)
        buttons.addWidget(self._watermark_remove)
        buttons.addStretch()
        image_row = QHBoxLayout()
        image_row.setContentsMargins(0, 0, 0, 0)
        image_row.setSpacing(metric.ENTRY_GAP)
        image_row.addWidget(self._watermark_thumb)
        image_row.addLayout(buttons)
        image_row.addStretch()
        image_widget = QWidget()
        image_widget.setLayout(image_row)

        self._watermark_image_note = QLabel()
        self._watermark_image_note.setTextFormat(Qt.TextFormat.PlainText)
        self._watermark_image_note.setWordWrap(True)
        self._watermark_image_note.setFont(_ui_font(11.5, 400))

        # A file chosen on this visit, copied in by Save; or, once the kept
        # image is removed, the removal Save will make.
        self._watermark_image_source: Path | None = None
        self._watermark_image_removed = False
        self._refresh_watermark_image()

        size_note = QLabel(
            f"Sized to the snip: {round(mark.MARK_H_SHARE * 100)}% of its shorter "
            f"side, between {mark.MARK_H_MIN} and {mark.MARK_H_MAX} pixels tall, "
            "and never stretched past an image's own pixels."
        )
        size_note.setWordWrap(True)
        size_note.setFont(_ui_font(11.5, 400))
        size_note.setStyleSheet(f"color: {win.TEXT_FAINT};")

        return _pane(
            SectionHeading("Watermark"),
            intro,
            None,
            SectionHeading("What the mark is"),
            self._watermark_cards["text"],
            self._watermark_text,
            *text_style,
            None,
            self._watermark_cards["image"],
            image_widget,
            self._watermark_image_note,
            None,
            SectionHeading("Size"),
            size_note,
        )

    def _watermark_text_style_rows(self) -> list[QWidget]:
        """The text mark's colour, font and plate, and a preview of it on a
        light and a dark ground -- a colour only means something against
        what it will land on, and a watermark lands on both."""
        win, metric = tokens.Win, tokens.WinMetric
        mark = tokens.WatermarkMetric

        def label(text: str) -> QLabel:
            widget = QLabel(text)
            widget.setFont(_ui_font(12.5, 500))
            widget.setStyleSheet(f"color: {win.TEXT_BODY};")
            widget.setFixedWidth(mark.STYLE_LABEL_W)
            return widget

        self._watermark_color = setup_desktop.load_watermark_color(self._config_dir)
        self._watermark_swatch_group = QButtonGroup(self)
        self._watermark_swatch_group.setExclusive(True)
        self._watermark_swatches: list[_ColorSwatch] = []
        colour_row = QHBoxLayout()
        colour_row.setContentsMargins(0, 0, 0, 0)
        colour_row.setSpacing(mark.SWATCH_GAP)
        colour_row.addWidget(label("Colour"))
        for name, value in tokens.WATERMARK_SWATCHES:
            swatch = _ColorSwatch(name, value)
            swatch.clicked.connect(lambda _c=False, v=value: self._pick_watermark_color(v))
            self._watermark_swatch_group.addButton(swatch)
            self._watermark_swatches.append(swatch)
            colour_row.addWidget(swatch)
        # A colour from the picker gets a swatch of its own, shown only
        # while it is one the presets do not already hold.
        self._watermark_custom_swatch = _ColorSwatch("Custom", self._watermark_color)
        self._watermark_custom_swatch.clicked.connect(
            lambda _c=False: self._pick_watermark_color(self._watermark_custom_swatch.color)
        )
        self._watermark_swatch_group.addButton(self._watermark_custom_swatch)
        colour_row.addWidget(self._watermark_custom_swatch)
        custom = SecondaryButton("Custom…")
        custom.clicked.connect(lambda _checked=False: self._choose_watermark_color())
        colour_row.addSpacing(mark.SWATCH_GAP)
        colour_row.addWidget(custom)
        colour_row.addStretch()
        colour_widget = QWidget()
        colour_widget.setLayout(colour_row)

        self._watermark_font = _FontCombo()
        self._watermark_font.setFont(_ui_font(12.5, 400))
        self._watermark_font.setFixedHeight(metric.CONTROL_H)
        self._watermark_font.setFixedWidth(mark.FONT_COMBO_W)
        self._watermark_font.setMaxVisibleItems(16)
        self._watermark_font.setStyleSheet(
            f"QComboBox {{ background: {win.FIELD_BG}; border: 1px solid {win.FIELD_BORDER};"
            f" border-radius: {metric.CONTROL_RADIUS}px; color: {win.TEXT_PRIMARY};"
            " padding: 0 11px; }"
            "QComboBox::drop-down { border: none; background: transparent;"
            f" width: {mark.FONT_COMBO_ARROW_W}px; }}"
            "QComboBox::down-arrow { image: none; }"
            f"QComboBox QAbstractItemView {{ background: {win.FIELD_BG};"
            f" color: {win.TEXT_PRIMARY}; selection-background-color: {win.ROW_HOVER}; }}"
        )
        self._watermark_font.addItem(tokens.WATERMARK_FONT_DEFAULT_LABEL, "")
        stored_font = setup_desktop.load_watermark_font(self._config_dir)
        families = sorted(
            {f for f in QFontDatabase.families() if not QFontDatabase.isPrivateFamily(f)},
            key=str.casefold,
        )
        # A family saved on a machine that no longer has it still shows, so
        # opening Settings never quietly swaps it for something else.
        if stored_font and stored_font not in families:
            families.insert(0, stored_font)
        for family in families:
            self._watermark_font.addItem(family, family)
        self._watermark_font.setCurrentIndex(max(0, self._watermark_font.findData(stored_font)))
        self._watermark_font.currentIndexChanged.connect(
            lambda _i: self._on_watermark_style_edited()
        )
        font_row = QHBoxLayout()
        font_row.setContentsMargins(0, 0, 0, 0)
        font_row.setSpacing(mark.SWATCH_GAP)
        font_row.addWidget(label("Font"))
        font_row.addWidget(self._watermark_font)
        font_row.addStretch()
        font_widget = QWidget()
        font_widget.setLayout(font_row)

        self._watermark_backing = SwitchRow(
            "Dark box behind the text",
            "Keeps light text readable on a light snip. Off, the text gets a "
            "thin edge in the opposite shade instead.",
            setup_desktop.load_watermark_backing(self._config_dir),
        )
        self._watermark_backing.switch.toggled.connect(
            lambda _c: self._on_watermark_style_edited()
        )

        self._watermark_preview = QLabel()
        self._watermark_preview.setFixedHeight(mark.PREVIEW_H)
        self._watermark_preview.setMinimumWidth(2 * mark.THUMB_W)
        self._watermark_preview.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        self._watermark_text.textChanged.connect(lambda _t: self._refresh_watermark_preview())

        self._refresh_watermark_swatches()
        return [colour_widget, font_widget, self._watermark_backing, self._watermark_preview]

    def _watermark_text_mark(self) -> Watermark:
        """The text mark as the page would save it, for the preview."""
        return Watermark(
            corner="tl",
            opacity=100,
            text=self._watermark_text.text() or self._watermark_text.placeholderText(),
            color=QColor(self._watermark_color),
            font_family=self._watermark_font.currentData() or "",
            backing=self._watermark_backing.switch.isChecked(),
        )

    def _refresh_watermark_preview(self) -> None:
        mark = tokens.WatermarkMetric
        ratio = self.devicePixelRatioF()
        width = max(self._watermark_preview.width(), 2 * mark.THUMB_W)
        height = mark.PREVIEW_H
        image = QImage(
            round(width * ratio), round(height * ratio), QImage.Format.Format_ARGB32_Premultiplied
        )
        image.setDevicePixelRatio(ratio)
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        half = width / 2
        radius = tokens.WinMetric.CONTROL_RADIUS
        watermark = self._watermark_text_mark()
        for index, ground in enumerate(("PREVIEW_LIGHT", "PREVIEW_DARK")):
            area = QRectF(index * half, 0, half, height)
            painter.setBrush(design.watermark_color(ground))
            painter.setPen(QPen(QColor(tokens.Win.FIELD_BORDER), 1))
            painter.drawRoundedRect(area.adjusted(1, 1, -1, -1), radius, radius)
            # Laid out as on a snip this size, at 1:1, then shifted so the
            # mark sits in the middle of its ground rather than its corner.
            placed = watermark.rect(area)
            if placed is not None:
                shift = area.center() - placed.center()
                watermark.paint(painter, area.translated(shift))
        painter.end()
        self._watermark_preview.setPixmap(QPixmap.fromImage(image))

    def _refresh_watermark_swatches(self) -> None:
        presets = {value.lower() for _name, value in tokens.WATERMARK_SWATCHES}
        is_custom = self._watermark_color.lower() not in presets
        if is_custom:
            self._watermark_custom_swatch.set_color(self._watermark_color)
        self._watermark_custom_swatch.setVisible(is_custom)
        for swatch in self._watermark_swatches:
            swatch.setChecked(swatch.color.lower() == self._watermark_color.lower())
        self._watermark_custom_swatch.setChecked(is_custom)
        self._refresh_watermark_preview()

    def _pick_watermark_color(self, color: str) -> None:
        self._watermark_color = QColor(color).name()
        self._refresh_watermark_swatches()
        self._on_watermark_style_edited()

    def _choose_watermark_color(self, color: QColor | str | None = None) -> None:
        """Pick any colour. `color` is only ever passed by tests: the
        colour dialog, like the file dialog, cannot be driven offscreen."""
        if color is None:
            color = QColorDialog.getColor(QColor(self._watermark_color), self, "Watermark colour")
        color = QColor(color)
        if not color.isValid():
            return
        self._pick_watermark_color(color.name())

    def _on_watermark_style_edited(self) -> None:
        # Styling the text is choosing to stamp text, as typing it is.
        self._watermark_cards["text"].setChecked(True)
        self._refresh_watermark_preview()
        self._mark_dirty()

    def _refresh_watermark_image(self) -> None:
        """Show the image the page would save: the one chosen on this visit,
        else the kept copy -- or say there is none, or that the kept copy is
        gone, which is the one case the overlay greys its button for that
        only this page can explain.
        """
        win, mark = tokens.Win, tokens.WatermarkMetric
        name: str | None = None
        image = QImage()
        missing = False
        if self._watermark_image_source is not None:
            name = self._watermark_image_source.name
            image = QImage(str(self._watermark_image_source))
        elif not self._watermark_image_removed:
            name = setup_desktop.load_watermark_image_name(self._config_dir)
            path = setup_desktop.load_watermark_image(self._config_dir)
            missing = name is not None and path is None
            if path is not None:
                image = QImage(str(path))

        self._watermark_remove.setEnabled(name is not None)
        self._watermark_thumb.setPixmap(QPixmap())
        if name is None:
            self._watermark_thumb.setText("No image")
            note, colour = "No image chosen yet. Pick a logo, or any picture.", win.TEXT_NOTE
        elif image.isNull():
            self._watermark_thumb.setText("Missing")
            problem = (
                "is no longer in Snipux's settings folder"
                if missing
                else "can no longer be read"
            )
            note = (
                f"{name} {problem}, so the watermark button stays greyed while "
                "Image is chosen. Choose the image again."
            )
            colour = win.ERR_FG
        else:
            self._watermark_thumb.setText("")
            ratio = self.devicePixelRatioF()
            room_w = (mark.THUMB_W - 2 * mark.THUMB_PAD) * ratio
            room_h = (mark.THUMB_H - 2 * mark.THUMB_PAD) * ratio
            shown = image
            # Shrunk to fit and never enlarged: a small logo shown blown up
            # would promise a sharper mark than it can make.
            if image.width() > room_w or image.height() > room_h:
                shown = image.scaled(
                    round(room_w),
                    round(room_h),
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            pixmap = QPixmap.fromImage(shown)
            pixmap.setDevicePixelRatio(ratio)
            self._watermark_thumb.setPixmap(pixmap)
            note = (
                f"{name} · {image.width()} × {image.height()} px. Snipux keeps "
                "its own copy, so moving or deleting the original does not lose it."
            )
            colour = win.TEXT_NOTE
        self._watermark_image_note.setText(note)
        self._watermark_image_note.setStyleSheet(f"color: {colour};")

    def _on_watermark_text_edited(self, _text: str) -> None:
        # Typing a mark is choosing to stamp text.
        self._watermark_cards["text"].setChecked(True)
        self._mark_dirty()

    def _choose_watermark_image(self, path: Path | str | None = None) -> None:
        """Pick the watermark image. `path` is only ever passed by tests:
        QFileDialog cannot be driven offscreen, the reason
        `ReviewWindow.save_as` takes one too.
        """
        if path is None:
            patterns = " ".join(
                sorted(
                    {
                        f"*.{bytes(fmt).decode()}"
                        for fmt in QImageReader.supportedImageFormats()
                    }
                )
            )
            chosen, _ = QFileDialog.getOpenFileName(
                self, "Choose a watermark image", str(Path.home()), f"Images ({patterns})"
            )
            if not chosen:
                return
            path = chosen
        path = Path(path)
        if QImage(str(path)).isNull():
            # Refused here, while the user is looking at it, rather than
            # saved and found out on the next snip as a greyed button.
            QMessageBox.warning(
                self,
                "Not an image",
                f"Snipux cannot read {path.name} as an image. Choose a PNG, a "
                "JPEG or another picture file.",
            )
            return
        self._watermark_image_source = path
        self._watermark_image_removed = False
        # Choosing an image is choosing to stamp one.
        self._watermark_cards["image"].setChecked(True)
        self._refresh_watermark_image()
        self._mark_dirty()

    def _remove_watermark_image(self) -> None:
        self._watermark_image_source = None
        self._watermark_image_removed = True
        self._refresh_watermark_image()
        self._mark_dirty()

    def _tray_pane(self) -> QWidget:
        self._tray_rows: dict[str, SwitchRow] = {}
        rows: list[QWidget] = [SectionHeading("Tray & startup")]
        stored = setup_desktop.load_tray_toggles(self._config_dir)
        for identifier, label, note, default in tokens.TRAY_TOGGLES:
            row = SwitchRow(label, note, stored.get(identifier, default))
            row.switch.toggled.connect(lambda _c: self._mark_dirty())
            self._tray_rows[identifier] = row
            rows.append(row)
        return _pane(*rows)

    # -- footer ----------------------------------------------------------

    def _build_footer_contents(self) -> None:
        self._dirty_label = QLabel()
        self._dirty_label.setFont(_ui_font(12, 400))
        self.footer_left.addWidget(self._dirty_label)

        cancel = SecondaryButton("Cancel")
        cancel.clicked.connect(self._cancel)
        self.footer_right.addWidget(cancel)

        # No tick on the label: it is a text glyph, so Windows paints it
        # from a symbol font that does not match the label beside it and
        # reads as ragged. A primary action does not need decoration --
        # Cancel next to it has none.
        save = AccentButton("Save")
        save.clicked.connect(self._save)
        self.footer_right.addWidget(save)

    def _refresh_dirty(self) -> None:
        if self._dirty:
            self._dirty_label.setText("Unsaved changes")
            self._dirty_label.setStyleSheet(f"color: {tokens.Win.WARN_FG};")
        else:
            self._dirty_label.setText("Everything saved")
            self._dirty_label.setStyleSheet(f"color: {tokens.Win.TEXT_FAINT};")

    def _mark_dirty(self) -> None:
        self._dirty = True
        self._refresh_dirty()

    # -- behaviour -------------------------------------------------------

    def _on_recorded(self, accelerator: str) -> None:
        self._conflict.show_for(accelerator)
        self._mark_dirty()

    def _choose_folder(self) -> None:
        chosen = QFileDialog.getExistingDirectory(self, "Save snips to", self._folder.text())
        if chosen:
            self._folder.setText(chosen)
            self._refresh_preview()
            self._mark_dirty()

    def _choose_recording_folder(self) -> None:
        chosen = QFileDialog.getExistingDirectory(
            self, "Save recordings to", self._recording_folder.text()
        )
        if chosen:
            self._recording_folder.setText(chosen)
            self._refresh_recording_preview()
            self._mark_dirty()

    def _refresh_recording_preview(self) -> None:
        """The recording twin of `_refresh_preview`.

        `extension="mp4"` rather than the stills default of "png" -- this
        label is a promise about a video's name, and on GNOME even the
        extension is not this app's to choose (Shell picks the container),
        so it is illustrative of the pattern, not of the container.
        """
        self._recording_preview.setText(
            setup_desktop.preview_filename(
                self._recording_folder.text(),
                self._recording_filename.text(),
                extension="mp4",
            )
        )
        if self.isVisible():
            self._mark_dirty()

    def _append_token(self, token: str) -> None:
        self._filename.setText(self._filename.text() + token)
        self._mark_dirty()

    def _refresh_preview(self) -> None:
        self._preview.setText(
            setup_desktop.preview_filename(self._folder.text(), self._filename.text())
        )
        if self.isVisible():
            self._mark_dirty()

    def _cancel(self) -> None:
        if self._dirty:
            answer = QMessageBox.question(
                self,
                "Discard changes?",
                "Settings have been changed but not saved. Discard them?",
                QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,
            )
            if answer != QMessageBox.StandardButton.Discard:
                return
        self.close()

    def _save(self) -> None:
        shortcut = self._recorder.shortcut_value()
        unchanged = setup_desktop.normalise_shortcut(shortcut) == setup_desktop.normalise_shortcut(
            setup_desktop.load_shortcut(self._config_dir)
        )
        # Only a *new* combination is checked. The probe cannot tell the
        # saved one apart from a registration this process failed to make
        # at startup, and refusing it then blocked every other setting too.
        if HotkeyEventFilter.is_available() and not unchanged:
            # Windows, unlike GNOME, can actually tell a taken combination
            # apart from a free one (see find_shortcut_conflict's own
            # docstring) -- so here, unlike the banner above, a clash is
            # refused outright rather than merely warned about: closing the
            # window and reporting the failure only afterwards would look
            # like a save that quietly did nothing.
            holder = platform.current.find_shortcut_conflict(shortcut)
            if holder is not None:
                QMessageBox.warning(
                    self,
                    "Shortcut already in use",
                    f"{shortcut} is already used by {holder} -- Snipux cannot "
                    "register it too. Choose a different combination.",
                )
                return
        complaints = self._hide_list_complaints()
        if complaints:
            # Saving a list that cannot work, and only finding out on the
            # next capture, is the failure this check exists to prevent --
            # the same reasoning as the shortcut clash above.
            QMessageBox.warning(
                self,
                "Check your hide list",
                "Snipux cannot use these lines:\n\n" + "\n".join(complaints),
            )
            return

        # Every save_* returns False rather than raising when the write
        # fails -- a read-only config directory, a full disk. Discarding
        # those returns meant Settings closed on "Everything saved" having
        # saved nothing, which is the one outcome worse than an error.
        written = [
            setup_desktop.save_shortcut(shortcut, self._config_dir),
            setup_desktop.save_after_capture(
                tokens.AFTER_CAPTURE[self._after_group.checkedId()][0], self._config_dir
            ),
            setup_desktop.save_instant_saves(
                self._instant_saves.switch.isChecked(), self._config_dir
            ),
            setup_desktop.save_save_folder(self._folder.text(), self._config_dir),
            setup_desktop.save_filename_pattern(self._filename.text(), self._config_dir),
            setup_desktop.save_native_resolution(
                self._native.switch.isChecked(), self._config_dir
            ),
            setup_desktop.save_recording_frame_rate(
                self._frame_rate.spin.value(), self._config_dir
            ),
            setup_desktop.save_recording_draw_cursor(
                self._draw_cursor.switch.isChecked(), self._config_dir
            ),
            setup_desktop.save_recording_after(
                tokens.RECORDING_AFTER[self._recording_after_group.checkedId()][0],
                self._config_dir,
            ),
            setup_desktop.save_recording_folder(
                self._recording_folder.text(), self._config_dir
            ),
            setup_desktop.save_recording_filename_pattern(
                self._recording_filename.text(), self._config_dir
            ),
            setup_desktop.save_default_tool(self._opening_tool.value(), self._config_dir),
            setup_desktop.save_default_ink(self._default_ink.value(), self._config_dir),
            setup_desktop.save_tool_defaults(self._tool_edits, self._config_dir),
            setup_desktop.save_remember_tool(
                self._remember_tool.switch.isChecked(), self._config_dir
            ),
            setup_desktop.save_hints_enabled(
                self._show_hints.switch.isChecked(), self._config_dir
            ),
            setup_desktop.save_watermark_kind(
                tokens.WATERMARK_KINDS[self._watermark_kind_group.checkedId()][0],
                self._config_dir,
            ),
            setup_desktop.save_watermark_text(self._watermark_text.text(), self._config_dir),
            setup_desktop.save_watermark_color(self._watermark_color, self._config_dir),
            setup_desktop.save_watermark_font(self._watermark_font.currentData() or "", self._config_dir),
            setup_desktop.save_watermark_backing(
                self._watermark_backing.switch.isChecked(), self._config_dir
            ),
        ]
        if self._watermark_image_source is not None:
            written.append(
                setup_desktop.save_watermark_image(
                    self._watermark_image_source, self._config_dir
                )
            )
        elif self._watermark_image_removed:
            written.append(setup_desktop.clear_watermark_image(self._config_dir))
        entries = self._hide_list_entries()
        written.append(
            setup_desktop.save_hide_list(
                entries["words"], entries["labels"], entries["patterns"], self._config_dir
            )
        )
        written.append(
            setup_desktop.save_tray_toggles(
                {key: row.switch.isChecked() for key, row in self._tray_rows.items()},
                self._config_dir,
            )
        )
        if not all(written):
            # Stays open and stays dirty: the window closing is what tells
            # the user their settings are somewhere safe, so a window that
            # closes on a failed write is the app lying about it. Every
            # value is still in the controls, so a fixed permission and a
            # second Save loses nothing.
            QMessageBox.warning(
                self,
                "Could not save your settings",
                f"Snipux could not write to {setup_desktop.config_path(self._config_dir)}.\n\n"
                "Check the folder still exists and is writable, then try again.",
            )
            return
        self._dirty = False
        self._refresh_dirty()
        if self._on_saved is not None:
            self._on_saved()
        self.close()


# `app.py` imports this name; the class was a QDialog before the redesign.
SettingsDialog = SettingsWindow
