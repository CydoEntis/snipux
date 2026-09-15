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

from PyQt6.QtCore import QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QFont, QKeyEvent, QKeySequence
from PyQt6.QtWidgets import (
    QButtonGroup,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from . import design, platform, setup_desktop
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

        self.setMinimumHeight(row.sizeHint().height())
        self.toggled.connect(lambda _checked: self._refresh())
        self._refresh()

    def _refresh(self) -> None:
        selected = self.isChecked()
        self._ring.set_selected(selected)
        self._title.setStyleSheet(
            f"color: {tokens.Win.TEXT_PRIMARY if selected else tokens.Win.TEXT_BODY};"
            " background: transparent;"
        )
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
        rail.setStyleSheet(
            f"background: {win.CHROME_BG}; border-right: 1px solid {win.SEPARATOR};"
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
        for build in (
            self._capture_pane,
            self._saving_pane,
            self._annotation_pane,
            self._hide_pane,
            self._tray_pane,
        ):
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
        # a preference nobody finds is a preference nobody has -- so it is a
        # toggle on the chooser row itself now, next to the mode it
        # modifies. See `chooser._RowToggle`.

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

        self._draw_cursor = SwitchRow(
            "Show the cursor in recordings",
            "Composites the mouse pointer into the video. Windows has no "
            "such toggle, so this only affects GNOME recordings.",
            setup_desktop.load_recording_draw_cursor(self._config_dir),
        )
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
        for index, (identifier, label, note) in enumerate(tokens.RECORDING_AFTER):
            card = RadioCard(label, note)
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
        note = QLabel(
            "These set the state the overlay opens in. The tools themselves "
            "are the overlay's own."
        )
        note.setWordWrap(True)
        note.setFont(_ui_font(11.5, 400))
        note.setStyleSheet(f"color: {tokens.Win.TEXT_NOTE};")

        self._remember_tool = SwitchRow(
            "Remember my last tool instead",
            "When on, the tool above is only the first-run seed.",
            setup_desktop.load_remember_tool(self._config_dir),
        )
        self._remember_tool.switch.toggled.connect(lambda _c: self._mark_dirty())

        self._show_hints = SwitchRow(
            "Show the hint bar",
            "Esc discard ink · Enter copy & close, across the top of the "
            "overlay. Off by default; press ? in the overlay to reveal it "
            "for one session without changing this.",
            setup_desktop.load_hints_enabled(self._config_dir),
        )
        self._show_hints.switch.toggled.connect(lambda _c: self._mark_dirty())

        return _pane(
            SectionHeading("Annotation"),
            note,
            None,
            self._remember_tool,
            self._show_hints,
        )

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
        if HotkeyEventFilter.is_available():
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

        setup_desktop.save_shortcut(shortcut, self._config_dir)
        setup_desktop.save_after_capture(
            tokens.AFTER_CAPTURE[self._after_group.checkedId()][0], self._config_dir
        )
        setup_desktop.save_instant_saves(
            self._instant_saves.switch.isChecked(), self._config_dir
        )
        setup_desktop.save_save_folder(self._folder.text(), self._config_dir)
        setup_desktop.save_filename_pattern(self._filename.text(), self._config_dir)
        setup_desktop.save_native_resolution(
            self._native.switch.isChecked(), self._config_dir
        )
        setup_desktop.save_recording_frame_rate(
            self._frame_rate.spin.value(), self._config_dir
        )
        setup_desktop.save_recording_draw_cursor(
            self._draw_cursor.switch.isChecked(), self._config_dir
        )
        setup_desktop.save_recording_after(
            tokens.RECORDING_AFTER[self._recording_after_group.checkedId()][0],
            self._config_dir,
        )
        setup_desktop.save_recording_folder(
            self._recording_folder.text(), self._config_dir
        )
        setup_desktop.save_recording_filename_pattern(
            self._recording_filename.text(), self._config_dir
        )
        setup_desktop.save_remember_tool(
            self._remember_tool.switch.isChecked(), self._config_dir
        )
        setup_desktop.save_hints_enabled(
            self._show_hints.switch.isChecked(), self._config_dir
        )
        entries = self._hide_list_entries()
        setup_desktop.save_hide_list(
            entries["words"], entries["labels"], entries["patterns"], self._config_dir
        )
        setup_desktop.save_tray_toggles(
            {key: row.switch.isChecked() for key, row in self._tray_rows.items()},
            self._config_dir,
        )
        self._dirty = False
        self._refresh_dirty()
        if self._on_saved is not None:
            self._on_saved()
        self.close()


# `app.py` imports this name; the class was a QDialog before the redesign.
SettingsDialog = SettingsWindow
