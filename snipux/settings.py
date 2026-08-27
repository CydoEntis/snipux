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
            self._tray_pane,
        ):
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QFrame.Shape.NoFrame)
            # Panes wrap; they never scroll sideways. A horizontal scrollbar
            # here hides the right-hand control of every row behind it.
            scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            scroll.setStyleSheet("background: transparent;")
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

    def _saving_pane(self) -> QWidget:
        folder_row = QHBoxLayout()
        self._folder = QLineEdit(str(setup_desktop.load_save_folder(self._config_dir)))
        self._folder.setReadOnly(True)
        self._folder.setFont(_mono_font(12))
        self._folder.setFixedHeight(tokens.WinMetric.CONTROL_H)
        self._folder.setStyleSheet(self._field_style())
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
        self._filename.setStyleSheet(self._field_style())
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
        self._recording_folder.setStyleSheet(self._field_style())
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
        self._recording_filename.setStyleSheet(self._field_style())
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

        save = AccentButton("✓  Save")
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

    @staticmethod
    def _field_style() -> str:
        win, metric = tokens.Win, tokens.WinMetric
        return (
            f"QLineEdit {{ background: {win.FIELD_BG};"
            f" border: 1px solid {win.FIELD_BORDER};"
            f" border-radius: {metric.CONTROL_RADIUS}px;"
            f" color: {win.TEXT_PRIMARY}; padding: 0 11px; }}"
        )

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
