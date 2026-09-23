"""Tests for `snipux/settings.py` -- the Settings window from
`docs/design/handoff-windows.md` section 2.
"""

from __future__ import annotations

import inspect
import re

import pytest
from PyQt6.QtCore import QEvent, QPointF, QSizeF, Qt
from PyQt6.QtGui import QColor, QFocusEvent, QImage, QKeyEvent, QMouseEvent
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QAbstractButton, QApplication, QLabel, QMessageBox

from snipux import platform, player, settings, setup_desktop
from snipux.design import tokens
from snipux import settings as settings_module
from snipux.settings import (
    ConflictBanner,
    EntryList,
    HotkeyEventFilter,
    SettingsWindow,
    ShortcutRecorder,
    _field_style,
    _rgba,
    accelerator_from_event,
)
from snipux.winchrome import SecondaryButton

# An internal tracker id (SNX-105, PROJ-42, ...) reads as a leaked note to
# anyone outside the team maintaining this -- AC: none may appear in text a
# user of the running app can actually see.
_TICKET_ID = re.compile(r"\b[A-Z]{2,6}-\d+\b")


@pytest.fixture(scope="module", autouse=True)
def qapp():
    # PyQt6 needs a live QApplication to construct any QWidget, even
    # offscreen. Module-scoped, same as test_overlay.py's own.
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


@pytest.fixture(autouse=True)
def _default_to_the_gnome_conflict_check(monkeypatch):
    """Every test in this file builds a `SettingsWindow`/`ConflictBanner`
    against whatever OS actually runs pytest -- forcing
    `HotkeyEventFilter.is_available()` off by default keeps that pinned to
    GNOME's own behaviour (the AC this file otherwise exists to protect,
    "the Linux behaviour of this window is unchanged") regardless of that
    host, rather than a `SettingsWindow()` on a Windows dev box silently
    starting to probe a real, system-wide hotkey on every construction.
    `test_app.py`'s `TestWindowsHotkeyIntegration` states this the same way,
    explicitly, per test, rather than relying on it -- the handful of tests
    below that want the Windows path override it back to `True`.
    """
    monkeypatch.setattr(HotkeyEventFilter, "is_available", staticmethod(lambda: False))


@pytest.fixture(autouse=True)
def _default_to_no_gif_encoder(monkeypatch):
    """Every test in this file builds a `SettingsWindow`, and its Recording
    pane now probes `player.system_ffmpeg()` to grey the GIF row -- pinned
    off by default, the same reasoning as the GNOME hotkey fixture above,
    so a test that isn't about GIF is not answered by whether this machine
    happens to have one. The handful below that are override it back on.
    """
    monkeypatch.setattr(player, "system_ffmpeg", lambda: None)


def press(key: Qt.Key, modifiers=Qt.KeyboardModifier.NoModifier) -> QKeyEvent:
    return QKeyEvent(QKeyEvent.Type.KeyPress, key, modifiers)


class TestAcceleratorFromEvent:
    """A key press -> the design's normalised `Control+Alt+S`."""

    def test_modifier_order_is_control_alt_shift_super(self):
        # Fixed by the design because this exact string is what the conflict
        # check and gsettings are both keyed on.
        event = press(
            Qt.Key.Key_S,
            Qt.KeyboardModifier.MetaModifier
            | Qt.KeyboardModifier.ShiftModifier
            | Qt.KeyboardModifier.ControlModifier,
        )

        assert accelerator_from_event(event) == "Control+Shift+Super+S"

    def test_letters_are_upper_cased(self):
        event = press(
            Qt.Key.Key_X, Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier
        )

        assert accelerator_from_event(event) == "Control+Alt+X"

    def test_named_keys_keep_their_capitalisation(self):
        event = press(Qt.Key.Key_Print, Qt.KeyboardModifier.AltModifier)

        assert accelerator_from_event(event) == "Alt+Print"

    @pytest.mark.parametrize(
        "key",
        [Qt.Key.Key_Shift, Qt.Key.Key_Control, Qt.Key.Key_Alt, Qt.Key.Key_Super_L],
    )
    def test_a_bare_modifier_is_not_a_shortcut_yet(self, key):
        # None means "keep listening" -- the first modifier down must not end
        # the capture on a meaningless accelerator.
        assert accelerator_from_event(press(key)) is None

    def test_a_key_with_no_modifier_is_refused(self):
        # It would swallow that key desktop-wide.
        assert accelerator_from_event(press(Qt.Key.Key_S)) is None

    def test_the_result_always_satisfies_the_cli_validator(self):
        # The window and `--setup --shortcut` must not disagree about what a
        # valid shortcut is.
        event = press(
            Qt.Key.Key_K,
            Qt.KeyboardModifier.MetaModifier | Qt.KeyboardModifier.ControlModifier,
        )

        assert setup_desktop.validate_shortcut(accelerator_from_event(event)) is None


class TestShortcutRecorder:
    def test_shows_the_current_shortcut(self):
        recorder = ShortcutRecorder("Control+Alt+S")

        assert "Control+Alt+S" in recorder._field.text()
        assert recorder._button.text() == "Record"

    def test_recording_changes_the_field_and_the_button(self):
        recorder = ShortcutRecorder(tokens.SHORTCUT_DEFAULT)

        recorder._start()

        assert recorder.is_recording()
        assert "Press a combination" in recorder._field.text()
        assert recorder._button.text() == "Cancel"

    def test_recording_a_combination_commits_and_stops(self):
        recorder = ShortcutRecorder(tokens.SHORTCUT_DEFAULT)
        recorded = []
        recorder.recorded.connect(recorded.append)
        recorder._start()

        recorder.keyPressEvent(press(Qt.Key.Key_J, Qt.KeyboardModifier.MetaModifier))

        assert recorded == ["Super+J"]
        assert recorder.shortcut_value() == "Super+J"
        assert not recorder.is_recording()

    def test_a_bare_modifier_does_not_end_recording(self):
        recorder = ShortcutRecorder(tokens.SHORTCUT_DEFAULT)
        recorder._start()

        recorder.keyPressEvent(press(Qt.Key.Key_Shift))

        assert recorder.is_recording()
        assert recorder.shortcut_value() == tokens.SHORTCUT_DEFAULT

    def test_escape_cancels_and_keeps_the_old_binding(self):
        recorder = ShortcutRecorder("Control+Alt+S")
        recorder._start()

        recorder.keyPressEvent(press(Qt.Key.Key_Escape))

        assert not recorder.is_recording()
        assert recorder.shortcut_value() == "Control+Alt+S"

    def test_keys_are_ignored_entirely_while_not_recording(self):
        recorder = ShortcutRecorder("Control+Alt+S")

        recorder.keyPressEvent(press(Qt.Key.Key_J, Qt.KeyboardModifier.MetaModifier))

        assert recorder.shortcut_value() == "Control+Alt+S"

    def test_the_pulsing_dot_only_exists_while_recording(self):
        # The design is explicit: render the dot only while recording, do not
        # fade a permanently-present one.
        recorder = ShortcutRecorder("Control+Alt+S")
        assert "●" not in recorder._field.text()

        recorder._start()

        assert "●" in recorder._field.text()


class TestConflictBanner:
    """`HotkeyEventFilter.is_available()` -- forced rather than relied on,
    same as `test_app.py`'s `TestWindowsHotkeyIntegration` -- is what picks
    GNOME's introspectable check apart from Windows' probe-based one
    (SNX-93), so this coverage holds regardless of which OS actually runs
    the suite.
    """

    def test_names_the_owner_of_a_clashing_shortcut(self, monkeypatch):
        monkeypatch.setattr(HotkeyEventFilter, "is_available", staticmethod(lambda: False))
        monkeypatch.setattr(
            setup_desktop,
            "find_shortcut_conflicts_named",
            lambda s: [("terminal", "GNOME’s “Launch terminal”")],
        )
        banner = ConflictBanner()

        banner.show_for("Control+Alt+T")

        text = banner.text()
        assert "Launch terminal" in text
        # A warning, not a block, and honest about GNOME's silence.
        assert "will not warn" in text

    def test_never_claims_a_shortcut_is_free(self, monkeypatch):
        # An application that grabs a key directly is invisible to the check.
        monkeypatch.setattr(HotkeyEventFilter, "is_available", staticmethod(lambda: False))
        monkeypatch.setattr(setup_desktop, "find_shortcut_conflicts_named", lambda s: [])
        banner = ConflictBanner()

        banner.show_for("Control+Alt+S")

        assert banner.text() == "✓  No GNOME shortcut uses Control+Alt+S."

    def test_on_windows_names_the_application_holding_a_taken_shortcut(self, monkeypatch):
        monkeypatch.setattr(HotkeyEventFilter, "is_available", staticmethod(lambda: True))
        monkeypatch.setattr(
            platform.current,
            "find_shortcut_conflict",
            lambda s: "another application",
            raising=False,
        )
        banner = ConflictBanner()

        banner.show_for("Control+Alt+T")

        text = banner.text()
        assert "another application" in text
        assert "Control+Alt+T" in text

    def test_on_windows_calls_out_the_snipping_tool(self, monkeypatch):
        # AC: Win+Shift+S is named as belonging to the Windows Snipping Tool.
        monkeypatch.setattr(HotkeyEventFilter, "is_available", staticmethod(lambda: True))
        monkeypatch.setattr(
            platform.current,
            "find_shortcut_conflict",
            lambda s: "the Windows Snipping Tool",
            raising=False,
        )
        banner = ConflictBanner()

        banner.show_for("Shift+Super+S")

        assert "the Windows Snipping Tool" in banner.text()

    def test_on_windows_never_claims_a_shortcut_is_free(self, monkeypatch):
        monkeypatch.setattr(HotkeyEventFilter, "is_available", staticmethod(lambda: True))
        monkeypatch.setattr(
            platform.current, "find_shortcut_conflict", lambda s: None, raising=False
        )
        banner = ConflictBanner()

        banner.show_for("Control+Alt+S")

        assert banner.text() == "✓  Control+Alt+S is free to register."


def left_click() -> QMouseEvent:
    return QMouseEvent(
        QEvent.Type.MouseButtonPress, QPointF(4, 4), QPointF(4, 4),
        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )


def focus_out() -> QFocusEvent:
    return QFocusEvent(QEvent.Type.FocusOut, Qt.FocusReason.MouseFocusReason)


class TestEntryList:
    """The Settings list control: short strings edited as rows, each with
    its own remove control, and an add field beneath."""

    @staticmethod
    def _list(*entries: str) -> EntryList:
        entry_list = EntryList("Add a word")
        entry_list.set_entries(list(entries))
        return entry_list

    @staticmethod
    def _changes(entry_list: EntryList) -> list:
        fired = []
        entry_list.changed.connect(lambda: fired.append(True))
        return fired

    @staticmethod
    def _add(entry_list: EntryList, text: str) -> None:
        entry_list._add_field.setText(text)
        entry_list._add_button.click()

    @staticmethod
    def _edit(entry_list: EntryList, index: int, text: str):
        row = entry_list._rows[index]
        row.mousePressEvent(left_click())
        row._editor.setText(text)
        return row

    # -- rows ------------------------------------------------------------

    def test_shows_one_row_per_entry(self):
        entry_list = self._list("Acme Corporation", "12 Maple Street")

        assert [row._text.full_text() for row in entry_list._rows] == [
            "Acme Corporation",
            "12 Maple Street",
        ]

    def test_the_add_field_carries_the_placeholder(self):
        assert EntryList("Add a pattern")._add_field.placeholderText() == "Add a pattern"

    def test_the_button_adds_what_is_typed(self):
        entry_list = self._list("Acme Corporation")

        self._add(entry_list, "12 Maple Street")

        assert entry_list.entries() == ["Acme Corporation", "12 Maple Street"]
        # Emptied, ready for the next one.
        assert entry_list._add_field.text() == ""

    def test_enter_in_the_add_field_adds_too(self):
        entry_list = self._list()
        entry_list._add_field.setText("Acme Corporation")

        entry_list._add_field.keyPressEvent(press(Qt.Key.Key_Return))

        assert entry_list.entries() == ["Acme Corporation"]

    def test_what_is_added_is_trimmed(self):
        entry_list = self._list()

        self._add(entry_list, "  Acme Corporation  ")

        assert entry_list.entries() == ["Acme Corporation"]

    # -- editing in place ------------------------------------------------

    def test_clicking_a_row_opens_that_entry_for_editing_in_place(self):
        entry_list = self._list("Acme Corporation", "12 Maple Street")
        row = entry_list._rows[1]

        row.mousePressEvent(left_click())

        assert row.is_editing()
        assert row._editor.text() == "12 Maple Street"
        # In place: the row's own text gives way to a field in the same row.
        assert not row._editor.isHidden()
        assert row._text.isHidden()
        assert not entry_list._rows[0].is_editing()

    def test_enter_commits_an_edit(self):
        entry_list = self._list("Acme Corporation")
        row = self._edit(entry_list, 0, "Acme Corp")

        row._editor.keyPressEvent(press(Qt.Key.Key_Return))

        assert entry_list.entries() == ["Acme Corp"]
        assert not row.is_editing()
        assert row._text.full_text() == "Acme Corp"

    def test_leaving_the_row_commits_an_edit(self):
        entry_list = self._list("Acme Corporation")
        row = self._edit(entry_list, 0, "Acme Corp")

        row._editor.focusOutEvent(focus_out())

        assert entry_list.entries() == ["Acme Corp"]
        assert not row.is_editing()

    def test_leaving_an_untouched_row_closes_it(self):
        # Qt's own editingFinished would not fire here -- the text did not
        # change -- and the row would be left open as a field.
        entry_list = self._list("Acme Corporation")
        row = entry_list._rows[0]
        row.mousePressEvent(left_click())

        row._editor.focusOutEvent(focus_out())

        assert not row.is_editing()
        assert row._editor.isHidden()

    def test_escape_abandons_an_edit(self):
        entry_list = self._list("Acme Corporation")
        changes = self._changes(entry_list)
        row = self._edit(entry_list, 0, "Acme Corp")

        row._editor.keyPressEvent(press(Qt.Key.Key_Escape))

        assert entry_list.entries() == ["Acme Corporation"]
        assert row._text.full_text() == "Acme Corporation"
        assert not row.is_editing()
        assert changes == []

    def test_the_focus_out_after_escape_does_not_commit_it_after_all(self):
        # Hiding the field on Escape moves focus off it in a real window;
        # that focus-out must not bring back the text Escape abandoned.
        entry_list = self._list("Acme Corporation")
        row = self._edit(entry_list, 0, "Acme Corp")

        row._editor.keyPressEvent(press(Qt.Key.Key_Escape))
        row._editor.focusOutEvent(focus_out())

        assert entry_list.entries() == ["Acme Corporation"]

    @pytest.mark.parametrize("text", ["", "   ", "12 Maple Street"])
    def test_an_edit_into_a_blank_or_a_duplicate_is_abandoned(self, text):
        # Clearing a row is not how an entry is removed, and two rows with
        # the same text could not be told apart by set_problems.
        entry_list = self._list("Acme Corporation", "12 Maple Street")
        changes = self._changes(entry_list)
        row = self._edit(entry_list, 0, text)

        row._editor.keyPressEvent(press(Qt.Key.Key_Return))

        assert entry_list.entries() == ["Acme Corporation", "12 Maple Street"]
        assert row._text.full_text() == "Acme Corporation"
        assert changes == []

    # -- removing ----------------------------------------------------------

    def test_each_row_removes_only_its_own_entry(self):
        entry_list = self._list("Acme Corporation", "12 Maple Street", "Project Nightjar")

        entry_list._rows[1]._remove.click()

        assert entry_list.entries() == ["Acme Corporation", "Project Nightjar"]
        assert len(entry_list._rows) == 2

    # -- entries, seeding and changed ----------------------------------------

    def test_entries_come_back_in_order(self):
        entry_list = self._list("Project Nightjar", "Acme Corporation")

        self._add(entry_list, "12 Maple Street")

        assert entry_list.entries() == [
            "Project Nightjar",
            "Acme Corporation",
            "12 Maple Street",
        ]

    def test_set_entries_replaces_rather_than_appends(self):
        entry_list = self._list("Acme Corporation")

        entry_list.set_entries(["12 Maple Street"])

        assert entry_list.entries() == ["12 Maple Street"]
        assert len(entry_list._rows) == 1

    def test_set_entries_keeps_the_list_free_of_blanks_and_repeats(self):
        entry_list = self._list(" Acme Corporation ", "", "Acme Corporation", "12 Maple Street")

        assert entry_list.entries() == ["Acme Corporation", "12 Maple Street"]

    def test_seeding_is_not_a_change(self):
        entry_list = EntryList("Add a word")
        changes = self._changes(entry_list)

        entry_list.set_entries(["Acme Corporation", "12 Maple Street"])
        entry_list.set_entries([])

        assert changes == []

    def test_reseeding_while_a_row_is_open_is_not_a_change_either(self):
        entry_list = self._list("Acme Corporation")
        changes = self._changes(entry_list)
        row = self._edit(entry_list, 0, "Acme Corp")

        entry_list.set_entries(["12 Maple Street"])
        row._editor.focusOutEvent(focus_out())

        assert entry_list.entries() == ["12 Maple Street"]
        assert changes == []

    def test_an_add_is_one_change(self):
        entry_list = self._list()
        changes = self._changes(entry_list)

        self._add(entry_list, "Acme Corporation")

        assert changes == [True]

    def test_an_edit_is_one_change_even_when_enter_is_followed_by_a_focus_out(self):
        entry_list = self._list("Acme Corporation")
        changes = self._changes(entry_list)
        row = self._edit(entry_list, 0, "Acme Corp")

        row._editor.keyPressEvent(press(Qt.Key.Key_Return))
        row._editor.focusOutEvent(focus_out())

        assert changes == [True]

    def test_a_removal_is_one_change(self):
        entry_list = self._list("Acme Corporation", "12 Maple Street")
        changes = self._changes(entry_list)

        entry_list._rows[0]._remove.click()

        assert changes == [True]

    def test_an_edit_that_changes_nothing_is_not_a_change(self):
        entry_list = self._list("Acme Corporation")
        changes = self._changes(entry_list)
        row = self._edit(entry_list, 0, "  Acme Corporation ")

        row._editor.keyPressEvent(press(Qt.Key.Key_Return))

        assert changes == []

    # -- problems --------------------------------------------------------

    def test_a_problem_is_shown_beneath_the_row_it_names(self):
        entry_list = self._list("Acme Corporation", "bob")

        entry_list.set_problems({"bob": "'bob' is too short"})

        acme, bob = entry_list._rows
        assert not bob._reason.isHidden()
        assert bob._reason.text() == "'bob' is too short"
        assert bob.layout().indexOf(bob._reason) > bob.layout().indexOf(bob._frame)
        assert acme._reason.isHidden()

    def test_a_row_with_a_problem_reads_as_wrong(self):
        entry_list = self._list("Acme Corporation", "bob")

        entry_list.set_problems({"bob": "'bob' is too short"})

        acme, bob = entry_list._rows
        assert _rgba("ERR_BORDER") in bob._frame.styleSheet()
        assert tokens.Win.ERR_FG in bob._text.styleSheet()
        assert _rgba("ERR_BORDER") not in acme._frame.styleSheet()
        assert tokens.Win.ERR_FG not in acme._text.styleSheet()

    def test_clearing_problems_clears_them(self):
        entry_list = self._list("bob")
        entry_list.set_problems({"bob": "'bob' is too short"})

        entry_list.set_problems({})

        bob = entry_list._rows[0]
        assert bob._reason.isHidden()
        assert _rgba("ERR_BORDER") not in bob._frame.styleSheet()

    def test_a_problem_is_shown_on_a_row_added_after_it_was_set(self):
        entry_list = self._list()
        entry_list.set_problems({"bob": "'bob' is too short"})

        self._add(entry_list, "bob")

        assert not entry_list._rows[0]._reason.isHidden()

    def test_an_entry_is_shown_as_text_not_markup(self):
        entry_list = self._list("<b>Acme</b>")

        assert entry_list._rows[0]._text.textFormat() == Qt.TextFormat.PlainText

    # -- refusing blanks and duplicates ------------------------------------

    @pytest.mark.parametrize("text", ["", "   "])
    def test_a_blank_entry_is_refused_by_the_add_field(self, text):
        entry_list = self._list("Acme Corporation")
        changes = self._changes(entry_list)

        entry_list._add_field.setText(text)
        entry_list._add_field.keyPressEvent(press(Qt.Key.Key_Return))

        assert not entry_list._add_button.isEnabled()
        assert entry_list.entries() == ["Acme Corporation"]
        assert changes == []

    def test_a_duplicate_is_refused_and_says_why(self):
        entry_list = self._list("Acme Corporation")
        changes = self._changes(entry_list)

        entry_list._add_field.setText(" Acme Corporation ")
        entry_list._add_field.keyPressEvent(press(Qt.Key.Key_Return))

        assert not entry_list._add_button.isEnabled()
        assert not entry_list._add_note.isHidden()
        assert entry_list.entries() == ["Acme Corporation"]
        assert changes == []
        # Left where it was typed, to be corrected rather than retyped.
        assert entry_list._add_field.text() == " Acme Corporation "

    def test_removing_the_original_lets_the_same_text_in(self):
        entry_list = self._list("Acme Corporation")
        entry_list._add_field.setText("Acme Corporation")

        entry_list._rows[0]._remove.click()

        assert entry_list._add_button.isEnabled()
        assert entry_list._add_note.isHidden()

    # -- the look ----------------------------------------------------------

    def test_the_add_row_reuses_the_settings_field_and_button(self):
        entry_list = self._list()

        assert isinstance(entry_list._add_button, SecondaryButton)
        assert entry_list._add_field.styleSheet() == _field_style()

    def test_its_sizes_are_the_tokens(self):
        # deviceIndependentSize, not grab().height(): grab() is physical
        # pixels, and a correct row reads 1.5x taller on a 1.5x display.
        metric = tokens.WinMetric
        entry_list = self._list("Acme Corporation", "bob")
        entry_list.set_problems({"bob": "'bob' is too short"})
        entry_list._rows[0].mousePressEvent(left_click())
        entry_list.grab()  # a full paintEvent, open field and problem row included

        row = entry_list._rows[1]
        assert row._frame.grab().deviceIndependentSize().height() == metric.ENTRY_ROW_H
        assert row._remove.grab().deviceIndependentSize() == QSizeF(
            metric.ENTRY_REMOVE, metric.ENTRY_REMOVE
        )
        assert entry_list._add_field.grab().deviceIndependentSize().height() == metric.CONTROL_H

    def test_no_literal_colour_anywhere_in_it(self):
        # AC: tokens.Win colours only. A hex typed into the control is the
        # one that is left behind when the palette changes.
        source = "".join(
            inspect.getsource(part)
            for part in (
                settings.EntryList,
                settings._EntryRow,
                settings._EntryText,
                settings._RowEditor,
                settings._field_style,
            )
        )

        assert not re.search(r"#[0-9a-fA-F]{3,8}\b", source)


class TestSettingsWindow:
    def _window(self, tmp_path, **kwargs):
        return SettingsWindow(config_dir=tmp_path, **kwargs)

    def test_is_the_size_the_design_specifies(self, tmp_path):
        window = self._window(tmp_path)

        assert window.width() == tokens.WinMetric.SETTINGS_W
        assert window.height() == tokens.WinMetric.SETTINGS_H

    def test_has_one_nav_row_per_settings_section(self, tmp_path):
        window = self._window(tmp_path)

        rows = window._nav_group.buttons()
        # "&&" is Qt's escape for a literal ampersand in a button label.
        assert [r.text().replace("&&", "&") for r in rows] == [
            label for _id, _icon, label in tokens.SETTINGS_NAV
        ]

    def test_capture_is_first_because_that_is_what_people_open_settings_for(self, tmp_path):
        window = self._window(tmp_path)

        assert window._nav_group.buttons()[0].text() == "Capture"
        assert window._nav_group.buttons()[0].isChecked()

    def test_opens_showing_the_stored_shortcut(self, tmp_path):
        setup_desktop.save_shortcut("Alt+Print", tmp_path)

        window = self._window(tmp_path)

        assert window._recorder.shortcut_value() == "Alt+Print"

    def test_after_capture_offers_three_behaviours_not_a_checkbox(self, tmp_path):
        # The old checkbox hid the other two entirely.
        window = self._window(tmp_path)

        assert len(window._after_group.buttons()) == len(tokens.AFTER_CAPTURE)

    def test_nothing_applies_until_save(self, tmp_path):
        window = self._window(tmp_path)
        window._recorder.set_shortcut("Control+Alt+K")

        assert setup_desktop.load_shortcut(tmp_path) == tokens.SHORTCUT_DEFAULT

    @staticmethod
    def _choose_after(window, identifier: str) -> None:
        """Pick an after-capture card by its id rather than its position:
        the list is ordered for reading, and that order is a display
        decision the rest of the app has no part in.
        """
        index = [row[0] for row in tokens.AFTER_CAPTURE].index(identifier)
        window._after_group.buttons()[index].setChecked(True)

    def test_save_commits_every_pane(self, tmp_path):
        window = self._window(tmp_path)
        window._recorder.set_shortcut("Control+Alt+K")
        self._choose_after(window, "review")
        window._instant_saves.switch.setChecked(True)
        window._filename.setText("shot-%Y")
        window._native.switch.setChecked(True)
        window._show_hints.switch.setChecked(True)

        window._save()

        assert setup_desktop.load_shortcut(tmp_path) == "Control+Alt+K"
        assert setup_desktop.load_after_capture(tmp_path) == "review"
        assert setup_desktop.load_instant_saves(tmp_path) is True
        assert setup_desktop.load_filename_pattern(tmp_path) == "shot-%Y"
        assert setup_desktop.load_native_resolution(tmp_path) is True
        assert setup_desktop.load_hints_enabled(tmp_path) is True

    def test_instant_saves_is_off_by_default(self, tmp_path):
        # AC (SNX-111): an upgrading user who never touches this switch
        # must not have Instant capture start writing files instead of
        # copying -- opening with nothing stored must show it off.
        window = self._window(tmp_path)

        assert window._instant_saves.switch.isChecked() is False

    def test_instant_saves_label_names_the_choice_it_makes(self, tmp_path):
        # AC: the label must say what the switch does, not "always copy
        # to clipboard too" -- that described a setting nothing read.
        window = self._window(tmp_path)

        labels = [c.text() for c in window._instant_saves.findChildren(QLabel)]
        assert "Save instead of copying" in labels
        note = next(t for t in labels if t != "Save instead of copying")
        assert "Capture and finish" in note

    def test_an_old_dead_always_copy_value_does_not_leak_into_the_new_switch(self, tmp_path):
        # A config written while this setting was still inert (SNX-111)
        # must load without error, and must not be read as the new
        # preference -- the two keys mean different things.
        setup_desktop.config_path(tmp_path).write_text('{"always_copy": true}')

        window = self._window(tmp_path)

        assert window._instant_saves.switch.isChecked() is False

    def test_the_hint_bar_toggle_offers_the_preference_snx_65_turned_off(self, tmp_path):
        # AC: Settings offers the hint-bar preference SNX-65 turned off by
        # default -- opening with nothing stored must show it off, matching
        # `load_hints_enabled`'s own documented default.
        window = self._window(tmp_path)

        assert window._show_hints.switch.isChecked() is False

    def test_the_hint_bar_description_says_what_it_does_not_why(self, tmp_path):
        # AC: the description names what the switch controls and how to see
        # it for one session, without citing the ticket that changed the
        # default -- that reasoning means nothing to a user reading Settings.
        window = self._window(tmp_path)

        # SwitchRow doesn't keep its note QLabel as a named attribute, so
        # find it the way a user would see it: the one other QLabel in the
        # row besides the title.
        labels = [c.text() for c in window._show_hints.findChildren(QLabel)]
        note = next(t for t in labels if t != "Show the hint bar")

        assert "press ? in the overlay" in note
        assert "Esc discard ink" in note
        assert not _TICKET_ID.search(note)

    def test_the_hint_bar_toggle_reads_back_through_the_named_setting(self, tmp_path):
        setup_desktop.save_hints_enabled(True, tmp_path)

        window = self._window(tmp_path)

        assert window._show_hints.switch.isChecked() is True

    def test_the_opening_tool_starts_on_the_pen(self, tmp_path):
        window = self._window(tmp_path)

        assert window._opening_tool.value() == "pen"
        assert window._opening_tool.text() == "Pen"

    def test_the_opening_tool_reads_back_what_was_stored(self, tmp_path):
        setup_desktop.save_default_tool("arrow", tmp_path)

        window = self._window(tmp_path)

        assert window._opening_tool.value() == "arrow"

    def test_every_tool_the_bar_can_open_with_is_offered_and_no_other(self, tmp_path):
        window = self._window(tmp_path)

        offered = window._opening_tool.values()

        assert offered == tokens.OPENING_TOOLS
        assert "eraser" not in offered and "eyedropper" not in offered
        assert tokens.OPENING_TOOL_NONE in offered

    def test_the_opening_tool_is_a_dropdown_listing_every_choice(self, tmp_path):
        window = self._window(tmp_path)

        window._opening_tool.click()
        menu = window._opening_tool.menu_widget()
        try:
            labels = [action.text() for action in menu.actions()]
            assert labels[0] == "Pen" and labels[-1] == "No tool"
            assert len(labels) == len(tokens.OPENING_TOOLS)
        finally:
            menu.close()

    def test_picking_an_opening_tool_is_an_unsaved_change_until_save(self, tmp_path):
        window = self._window(tmp_path)
        window._opening_tool.click()
        menu = window._opening_tool.menu_widget()

        menu.actions()[-1].trigger()  # No tool
        menu.close()

        assert window._dirty
        assert setup_desktop.load_default_tool(tmp_path) == "pen"

        window._save()

        assert setup_desktop.load_default_tool(tmp_path) == tokens.OPENING_TOOL_NONE

    def test_save_calls_back_so_the_shortcut_can_be_rebound(self, tmp_path):
        # Remembering a shortcut is not binding it: GNOME only knows about
        # the binding it was told.
        called = []
        window = self._window(tmp_path, on_saved=lambda: called.append(True))

        window._save()

        assert called == [True]

    def test_save_refuses_a_taken_shortcut_on_windows(self, tmp_path, monkeypatch):
        # AC: a combination that is already taken is refused with a message
        # naming what holds it, rather than appearing to save.
        setup_desktop.save_shortcut("Control+Alt+S", tmp_path)
        called = []
        window = self._window(tmp_path, on_saved=lambda: called.append(True))
        window._recorder.set_shortcut("Control+Alt+K")
        monkeypatch.setattr(HotkeyEventFilter, "is_available", staticmethod(lambda: True))
        monkeypatch.setattr(
            platform.current,
            "find_shortcut_conflict",
            lambda s: "another application",
            raising=False,
        )
        warnings = []
        monkeypatch.setattr(
            QMessageBox,
            "warning",
            lambda parent, title, text: warnings.append(text),
        )

        window._save()

        # Nothing committed and nothing rebound -- a refusal, not a save.
        assert setup_desktop.load_shortcut(tmp_path) == "Control+Alt+S"
        assert called == []
        assert len(warnings) == 1
        assert "another application" in warnings[0]
        assert "Control+Alt+K" in warnings[0]

    def test_an_unchanged_shortcut_never_blocks_the_other_settings(self, tmp_path, monkeypatch):
        # When the running app could not grab its own shortcut at startup,
        # the probe reports the saved combination as taken. Refusing then
        # made every Save a no-op -- watermark, hide list and all -- over a
        # shortcut the user had not touched.
        setup_desktop.save_shortcut("Control+Alt+S", tmp_path)
        window = self._window(tmp_path)
        monkeypatch.setattr(HotkeyEventFilter, "is_available", staticmethod(lambda: True))
        monkeypatch.setattr(
            platform.current,
            "find_shortcut_conflict",
            lambda s: "another application",
            raising=False,
        )
        warnings = []
        monkeypatch.setattr(
            QMessageBox,
            "warning",
            lambda parent, title, text: warnings.append(text),
        )
        window._watermark_text.setText("Mine")
        window._on_watermark_text_edited("Mine")

        window._save()

        assert warnings == []
        assert setup_desktop.load_watermark_text(tmp_path) == "Mine"
        assert setup_desktop.load_shortcut(tmp_path) == "Control+Alt+S"

    def test_save_calls_out_the_snipping_tool_by_name(self, tmp_path, monkeypatch):
        # AC: Win+Shift+S is called out as belonging to the Windows
        # Snipping Tool if the user tries it.
        window = self._window(tmp_path)
        window._recorder.set_shortcut("Shift+Super+S")
        monkeypatch.setattr(HotkeyEventFilter, "is_available", staticmethod(lambda: True))
        monkeypatch.setattr(
            platform.current,
            "find_shortcut_conflict",
            lambda s: "the Windows Snipping Tool",
            raising=False,
        )
        warnings = []
        monkeypatch.setattr(
            QMessageBox,
            "warning",
            lambda parent, title, text: warnings.append(text),
        )

        window._save()

        assert "the Windows Snipping Tool" in warnings[0]

    def test_save_proceeds_when_windows_reports_no_conflict(self, tmp_path, monkeypatch):
        window = self._window(tmp_path)
        window._recorder.set_shortcut("Control+Alt+K")
        monkeypatch.setattr(HotkeyEventFilter, "is_available", staticmethod(lambda: True))
        monkeypatch.setattr(
            platform.current, "find_shortcut_conflict", lambda s: None, raising=False
        )

        window._save()

        assert setup_desktop.load_shortcut(tmp_path) == "Control+Alt+K"

    def test_the_footer_reports_unsaved_changes(self, tmp_path):
        window = self._window(tmp_path)
        assert "Everything saved" in window._dirty_label.text()

        window._mark_dirty()

        assert "Unsaved changes" in window._dirty_label.text()

    def test_saving_clears_the_dirty_state(self, tmp_path):
        window = self._window(tmp_path)
        window._mark_dirty()

        window._save()

        assert "Everything saved" in window._dirty_label.text()

    def test_the_nav_rail_footer_wraps_rather_than_clips_the_version_line(self, tmp_path):
        # AC: not clipped at the width the panel gives it. The nav rail is
        # a fixed width (tokens.WinMetric.NAV_W) the label can't grow past,
        # so a version/Qt/platform line too long for one line at that width
        # must wrap onto another rather than being cut off mid-word, the
        # way the ticket's "Qt 6.11.0 - unknow" was.
        window = self._window(tmp_path)
        label = window._version_label
        window.grab()  # a full paintEvent -- CLAUDE.md's offscreen pattern

        assert label.wordWrap() is True

        # Driven with a line deliberately too long for the rail, rather
        # than with whatever this machine's own version line happens to
        # be. `version_line()` varies by package version, Qt version and
        # session type -- "x11" is short enough to fit on one line, so
        # asserting that the real line overflows made this test pass or
        # fail by machine state, which is the thing SNX-126 went and
        # removed everywhere else.
        label.setText("Snipux 0.1.0 / Qt 6.11.0 · a session with a long name")
        window.grab()

        # Proves wrapping is load-bearing here, not just set and unused:
        # the text is wider than the width the rail actually granted the
        # label, so without word wrap this exact case would still clip.
        single_line_width = label.fontMetrics().horizontalAdvance(label.text())
        assert single_line_width > label.width()
        # And it genuinely wraps rather than clipping: laid out at the
        # width it has, it needs more than one line's height.
        assert label.heightForWidth(label.width()) > label.fontMetrics().height()

    def test_the_filename_preview_shows_a_real_path(self, tmp_path):
        window = self._window(tmp_path)

        window._filename.setText("snip-%Y")

        preview = window._preview.text()
        assert preview.endswith(".png")
        assert "snip-" in preview

    def test_review_window_reads_back_through_the_named_setting(self, tmp_path):
        # app.py asks "is the review window on"; Settings stores one of three
        # behaviours. The two must agree.
        window = self._window(tmp_path)
        self._choose_after(window, "review")

        window._save()

        assert setup_desktop.load_review_window(tmp_path) is True

    @staticmethod
    def _choose_recording_after(window, identifier: str) -> None:
        index = [row[0] for row in tokens.RECORDING_AFTER].index(identifier)
        window._recording_after_group.buttons()[index].setChecked(True)

    def test_recording_destination_round_trips_through_settings(self, tmp_path):
        # The point of the row: recording's destination could only be set on
        # the chooser, per-capture. There was no way to say what it should
        # default to.
        window = self._window(tmp_path)

        self._choose_recording_after(window, "save")
        window._save()

        assert setup_desktop.load_recording_after(tmp_path) == "save"

    def test_gif_is_greyed_without_an_encoder(self, tmp_path):
        # Qt's QImageWriter carries no GIF plugin, so this needs a system
        # ffmpeg -- greyed with its reason exactly as the player's own
        # export row greys, not merely left out (a missing row reads as a
        # bug; a greyed one reads as a limit).
        window = self._window(tmp_path)

        card = window._recording_after_group.buttons()[
            [row[0] for row in tokens.RECORDING_AFTER].index("gif")
        ]

        assert card.isEnabled() is False
        assert card.isChecked() is False
        assert player.EXPORT_UNAVAILABLE["gif"] in card._sub.text()

    def test_gif_is_pickable_with_an_encoder(self, monkeypatch, tmp_path):
        monkeypatch.setattr(player, "system_ffmpeg", lambda: "/usr/bin/ffmpeg")
        window = self._window(tmp_path)

        card = window._recording_after_group.buttons()[
            [row[0] for row in tokens.RECORDING_AFTER].index("gif")
        ]
        assert card.isEnabled() is True

        self._choose_recording_after(window, "gif")
        window._save()

        assert setup_desktop.load_recording_after(tmp_path) == "gif"

    def test_a_stored_gif_default_falls_back_without_an_encoder(self, tmp_path):
        # Stored on a machine that had ffmpeg, opened on one that does not:
        # nothing here re-checks that box, so every card would otherwise
        # open unchecked -- and saving that state would hand
        # `QButtonGroup.checkedId()`'s -1 straight to `tokens.RECORDING_AFTER`,
        # which Python reads as *the last row* rather than "nothing picked".
        setup_desktop.save_recording_after("gif", tmp_path)

        window = self._window(tmp_path)
        window._save()

        assert setup_desktop.load_recording_after(tmp_path) == tokens.RECORD_AFTER_DEFAULT

    def test_recording_folder_and_pattern_round_trip(self, tmp_path):
        window = self._window(tmp_path)
        destination = tmp_path / "clips"

        window._recording_folder.setText(str(destination))
        window._recording_filename.setText("Clip %Y")
        window._save()

        assert setup_desktop.load_recording_folder(tmp_path) == destination
        assert setup_desktop.load_recording_filename_pattern(tmp_path) == "Clip %Y"

    def test_the_recording_preview_names_a_video_not_a_screenshot(self, tmp_path):
        # The recording pane's preview must be its own: sharing the stills
        # one is exactly how a video came to be named "Screenshot from
        # ....mp4", and a preview that showed .png would keep promising it.
        window = self._window(tmp_path)

        window._recording_filename.setText("Clip %Y")

        preview = window._recording_preview.text()
        assert preview.endswith(".mp4")
        assert "Clip " in preview
        assert preview != window._preview.text()

    def test_the_recording_rows_do_not_disturb_the_stills_ones(self, tmp_path):
        # Two sets of folder/filename rows on one pane, and they must stay
        # independent -- the whole complaint was that recordings and stills
        # shared one.
        window = self._window(tmp_path)

        window._recording_folder.setText(str(tmp_path / "clips"))
        window._recording_filename.setText("Clip %Y")
        window._save()

        assert setup_desktop.load_save_folder(tmp_path) != (tmp_path / "clips")
        assert setup_desktop.load_filename_pattern(tmp_path) != "Clip %Y"

    def test_recording_rows_seed_from_the_stored_values(self, tmp_path):
        # AC: both new rows are seeded from setup_desktop's own loads, not
        # some independent default the window invents.
        setup_desktop.save_recording_frame_rate(24, tmp_path)
        setup_desktop.save_recording_draw_cursor(False, tmp_path)

        window = self._window(tmp_path)

        assert window._frame_rate.spin.value() == 24
        assert window._draw_cursor.switch.isChecked() is False

    def test_recording_rows_default_to_the_documented_values(self, tmp_path):
        window = self._window(tmp_path)

        assert window._frame_rate.spin.value() == tokens.RECORDING_FRAME_RATE_DEFAULT
        assert window._draw_cursor.switch.isChecked() is True

    def test_changing_the_frame_rate_marks_the_window_dirty(self, tmp_path):
        window = self._window(tmp_path)

        window._frame_rate.spin.setValue(15)

        assert "Unsaved changes" in window._dirty_label.text()

    def test_changing_draw_cursor_marks_the_window_dirty(self, tmp_path):
        window = self._window(tmp_path)

        window._draw_cursor.switch.setChecked(False)

        assert "Unsaved changes" in window._dirty_label.text()

    def test_save_persists_the_recording_rows(self, tmp_path):
        window = self._window(tmp_path)
        window._frame_rate.spin.setValue(15)
        window._draw_cursor.switch.setChecked(False)

        window._save()

        assert setup_desktop.load_recording_frame_rate(tmp_path) == 15
        assert setup_desktop.load_recording_draw_cursor(tmp_path) is False

    def test_no_visible_text_in_the_window_names_a_ticket(self, tmp_path):
        # AC: a test fails if any user-facing string contains a ticket
        # identifier. Every pane is built eagerly in __init__ (see
        # _build_panes), so a single window instance covers all four,
        # including whichever isn't the one currently on top.
        window = self._window(tmp_path)

        texts = [window.windowTitle()]
        for label in window.findChildren(QLabel):
            texts.append(label.text())
        for button in window.findChildren(QAbstractButton):
            texts.append(button.text())
            texts.append(button.toolTip())

        offenders = [t for t in texts if t and _TICKET_ID.search(t)]
        assert offenders == []


class TestTheHideSensitivePage:
    """The user's own hide list, edited in Snipux rather than in a text
    editor: the file is no use if nobody can find it."""

    def _window(self, tmp_path, **kwargs):
        return SettingsWindow(config_dir=tmp_path, **kwargs)

    def _no_dialogs(self, monkeypatch) -> list:
        warnings = []
        monkeypatch.setattr(
            QMessageBox, "warning", lambda parent, title, text: warnings.append(text)
        )
        return warnings

    def test_the_rail_has_a_page_for_it(self, tmp_path):
        window = self._window(tmp_path)

        labels = [r.text().replace("&&", "&") for r in window._nav_group.buttons()]

        assert "Hide sensitive" in labels

    @staticmethod
    def _add(window, section: str, *entries: str) -> None:
        """Type each entry into that list's add field and press Add -- the
        way a user builds a list."""
        rows = window._hide_lists[section].rows
        for entry in entries:
            rows._add_field.setText(entry)
            rows._add_button.click()

    @staticmethod
    def _as_text(window, section: str):
        window._hide_lists[section].as_text.click()
        return window._hide_lists[section].text

    def test_it_opens_showing_what_is_stored(self, tmp_path):
        setup_desktop.save_hide_list(
            ["Acme Corporation", "12 Maple Street"], ["Employee ID"], [r"ACME-\d{6}"], tmp_path
        )

        window = self._window(tmp_path)

        assert window._hide_lists["words"].rows.entries() == [
            "Acme Corporation",
            "12 Maple Street",
        ]
        assert window._hide_lists["labels"].rows.entries() == ["Employee ID"]
        assert window._hide_lists["patterns"].rows.entries() == [r"ACME-\d{6}"]

    def test_a_list_for_each_kind_of_entry(self, tmp_path):
        window = self._window(tmp_path)

        assert set(window._hide_lists) == {section for section, _t, _n in tokens.HIDE_LIST_FIELDS}
        assert all(isinstance(each.rows, EntryList) for each in window._hide_lists.values())

    def test_each_list_opens_as_rows_rather_than_text(self, tmp_path):
        window = self._window(tmp_path)

        for each in window._hide_lists.values():
            assert not each.rows.isHidden()
            assert each.text.isHidden()
            assert each.as_text.text() == "Edit as text"

    def test_adding_an_entry_marks_the_window_dirty(self, tmp_path):
        window = self._window(tmp_path)
        assert window._dirty is False

        self._add(window, "words", "Acme Corporation")

        assert window._dirty is True

    def test_removing_an_entry_marks_the_window_dirty(self, tmp_path):
        setup_desktop.save_hide_list(["Acme Corporation"], [], [], tmp_path)
        window = self._window(tmp_path)

        window._hide_lists["words"].rows._rows[0]._remove.click()

        assert window._dirty is True

    def test_typing_in_the_text_marks_the_window_dirty(self, tmp_path):
        window = self._window(tmp_path)
        text = self._as_text(window, "words")

        text.setPlainText("Acme Corporation")

        assert window._dirty is True

    def test_swapping_to_text_and_back_is_not_an_edit(self, tmp_path):
        setup_desktop.save_hide_list(["Acme Corporation"], [], [], tmp_path)
        window = self._window(tmp_path)

        self._as_text(window, "words")
        window._hide_lists["words"].as_text.click()

        assert window._dirty is False

    def test_nothing_is_written_until_save(self, tmp_path):
        window = self._window(tmp_path)

        self._add(window, "words", "Acme Corporation")
        self._as_text(window, "labels").setPlainText("Employee ID")

        assert not setup_desktop.hide_list_path(tmp_path).exists()

    def test_save_writes_all_three_lists(self, tmp_path, monkeypatch):
        self._no_dialogs(monkeypatch)
        window = self._window(tmp_path)
        self._add(window, "words", "Acme Corporation", "12 Maple Street")
        self._add(window, "labels", "Employee ID")
        self._add(window, "patterns", r"ACME-\d{6}")

        window._save()

        assert setup_desktop.load_hide_list(tmp_path) == {
            "words": ["Acme Corporation", "12 Maple Street"],
            "labels": ["Employee ID"],
            "patterns": [r"ACME-\d{6}"],
        }

    def test_the_file_on_disk_does_not_change_shape(self, tmp_path, monkeypatch):
        # A list edited as rows is still the hide-list.txt someone can edit
        # by hand: exactly what save_hide_list writes for the same entries,
        # sections, notes and one entry per line.
        self._no_dialogs(monkeypatch)
        through_the_window, by_hand = tmp_path / "window", tmp_path / "hand"
        window = self._window(through_the_window)
        self._add(window, "words", "Acme Corporation", "12 Maple Street")
        self._add(window, "labels", "Employee ID")
        self._as_text(window, "patterns").setPlainText(r"ACME-\d{6}")

        window._save()
        setup_desktop.save_hide_list(
            ["Acme Corporation", "12 Maple Street"], ["Employee ID"], [r"ACME-\d{6}"], by_hand
        )

        written = setup_desktop.hide_list_path(through_the_window).read_text(encoding="utf-8")
        assert written == setup_desktop.hide_list_path(by_hand).read_text(encoding="utf-8")
        lines = written.splitlines()
        assert lines.index("[words]") < lines.index("Acme Corporation") < lines.index("[labels]")
        assert lines.index("[labels]") < lines.index("Employee ID") < lines.index("[patterns]")
        assert lines.index("[patterns]") < lines.index(r"ACME-\d{6}")

    def test_a_bad_entry_is_called_out_on_its_own_row(self, tmp_path):
        window = self._window(tmp_path)

        self._add(window, "words", "Acme Corporation", "bob")

        acme, bob = window._hide_lists["words"].rows._rows
        # `isHidden`, not `isVisible`: this page is not the one on screen
        # when Settings opens, so nothing on it is visible yet.
        assert not bob._reason.isHidden()
        assert "bob" in bob._reason.text()
        assert acme._reason.isHidden()

    def test_a_complaint_is_shown_in_its_own_list_not_under_all_three(self, tmp_path):
        window = self._window(tmp_path)

        self._add(window, "words", "bob")
        self._add(window, "patterns", "ACME-[0-9")

        (bob,) = window._hide_lists["words"].rows._rows
        (pattern,) = window._hide_lists["patterns"].rows._rows
        assert "bob" in bob._reason.text() and "ACME" not in bob._reason.text()
        assert "ACME-[0-9" in pattern._reason.text() and "bob" not in pattern._reason.text()
        assert window._hide_lists["labels"].complaints.isHidden()

    def test_the_same_entry_is_judged_by_the_list_it_is_in(self, tmp_path):
        # Too short to be a word, but a pattern is judged by what it
        # matches rather than by its length.
        window = self._window(tmp_path)

        self._add(window, "words", "abc")
        self._add(window, "patterns", "abc")

        assert not window._hide_lists["words"].rows._rows[0]._reason.isHidden()
        assert window._hide_lists["patterns"].rows._rows[0]._reason.isHidden()

    def test_the_complaint_goes_away_once_it_is_fixed(self, tmp_path):
        window = self._window(tmp_path)
        self._add(window, "words", "bob")
        row = window._hide_lists["words"].rows._rows[0]

        row.mousePressEvent(left_click())
        row._editor.setText("bob.smith@example.com")
        row._editor.keyPressEvent(press(Qt.Key.Key_Return))

        assert row._reason.text() == ""
        assert row._reason.isHidden()

    def test_a_bad_entry_already_in_the_file_is_called_out_on_opening(self, tmp_path):
        # A hand-edited file can hold a line Save would refuse; the row it
        # is on should say so before the user reaches Save and finds out.
        setup_desktop.save_hide_list(["bob"], [], [], tmp_path)

        window = self._window(tmp_path)

        assert not window._hide_lists["words"].rows._rows[0]._reason.isHidden()
        assert window._dirty is False

    def test_save_refuses_while_an_entry_cannot_work(self, tmp_path, monkeypatch):
        warnings = self._no_dialogs(monkeypatch)
        window = self._window(tmp_path)
        self._add(window, "patterns", "ACME-[0-9")

        window._save()

        assert len(warnings) == 1
        assert "ACME-[0-9" in warnings[0]
        assert setup_desktop.load_hide_list(tmp_path)["patterns"] == []

    def test_save_refuses_a_bad_line_in_the_text_too(self, tmp_path, monkeypatch):
        warnings = self._no_dialogs(monkeypatch)
        window = self._window(tmp_path)
        self._as_text(window, "words").setPlainText("Acme Corporation\nbob")

        window._save()

        assert len(warnings) == 1
        assert "bob" in warnings[0]
        assert setup_desktop.load_hide_list(tmp_path)["words"] == []

    def test_save_goes_ahead_once_the_bad_entry_is_removed(self, tmp_path, monkeypatch):
        warnings = self._no_dialogs(monkeypatch)
        window = self._window(tmp_path)
        self._add(window, "words", "Acme Corporation", "bob")

        window._hide_lists["words"].rows._rows[1]._remove.click()
        window._save()

        assert warnings == []
        assert setup_desktop.load_hide_list(tmp_path)["words"] == ["Acme Corporation"]

    def test_a_refusal_saves_nothing_at_all(self, tmp_path, monkeypatch):
        # Not even the settings on other pages: a half-applied Save is worse
        # than a refused one.
        self._no_dialogs(monkeypatch)
        window = self._window(tmp_path)
        window._recorder.set_shortcut("Control+Alt+K")
        self._add(window, "words", "bob")

        window._save()

        assert setup_desktop.load_shortcut(tmp_path) == tokens.SHORTCUT_DEFAULT

    def test_it_says_where_the_file_is(self, tmp_path):
        window = self._window(tmp_path)

        assert window._hide_file.text() == str(setup_desktop.hide_list_path(tmp_path))
        assert window._hide_file.isReadOnly()

    # -- Edit as text ------------------------------------------------------

    def test_edit_as_text_holds_the_same_entries_one_per_line(self, tmp_path):
        setup_desktop.save_hide_list(["Acme Corporation", "12 Maple Street"], [], [], tmp_path)
        window = self._window(tmp_path)

        text = self._as_text(window, "words")

        section = window._hide_lists["words"]
        assert text.toPlainText() == "Acme Corporation\n12 Maple Street"
        assert not text.isHidden()
        assert section.rows.isHidden()
        assert section.as_text.text() == "Edit as list"

    def test_each_list_swaps_on_its_own(self, tmp_path):
        window = self._window(tmp_path)

        self._as_text(window, "words")

        assert window._hide_lists["labels"].text.isHidden()
        assert window._hide_lists["patterns"].text.isHidden()

    def test_swapping_back_keeps_what_was_typed(self, tmp_path):
        setup_desktop.save_hide_list(["Acme Corporation"], [], [], tmp_path)
        window = self._window(tmp_path)
        text = self._as_text(window, "words")
        text.setPlainText("Acme Corporation\n12 Maple Street\nProject Nightjar")

        window._hide_lists["words"].as_text.click()

        section = window._hide_lists["words"]
        assert section.rows.entries() == ["Acme Corporation", "12 Maple Street", "Project Nightjar"]
        assert not section.rows.isHidden()
        assert section.text.isHidden()

    def test_pasted_blanks_and_repeats_are_tidied_on_the_way_back(self, tmp_path):
        window = self._window(tmp_path)
        text = self._as_text(window, "words")
        text.setPlainText("\n  Acme Corporation  \n\nAcme Corporation\n")

        window._hide_lists["words"].as_text.click()

        assert window._hide_lists["words"].rows.entries() == ["Acme Corporation"]

    def test_save_while_editing_as_text_writes_the_text(self, tmp_path, monkeypatch):
        # Nobody should have to swap back for Save to see what they pasted.
        self._no_dialogs(monkeypatch)
        window = self._window(tmp_path)
        self._as_text(window, "labels").setPlainText("Employee ID\nBadge number")

        window._save()

        assert setup_desktop.load_hide_list(tmp_path)["labels"] == ["Employee ID", "Badge number"]

    def test_blank_lines_and_spacing_survive_a_round_trip(self, tmp_path, monkeypatch):
        self._no_dialogs(monkeypatch)
        window = self._window(tmp_path)
        self._as_text(window, "words").setPlainText("\n  Acme Corporation  \n\n")

        window._save()

        assert setup_desktop.load_hide_list(tmp_path)["words"] == ["Acme Corporation"]

    def test_while_editing_as_text_a_complaint_is_shown_under_that_text(self, tmp_path):
        window = self._window(tmp_path)

        self._as_text(window, "words").setPlainText("Acme Corporation\nbob")

        section = window._hide_lists["words"]
        assert not section.complaints.isHidden()
        assert "bob" in section.complaints.text()
        assert window._hide_lists["labels"].complaints.isHidden()

    def test_on_the_way_back_the_complaint_moves_onto_its_row(self, tmp_path):
        window = self._window(tmp_path)
        self._as_text(window, "words").setPlainText("bob")

        window._hide_lists["words"].as_text.click()

        section = window._hide_lists["words"]
        assert section.complaints.isHidden()
        assert not section.rows._rows[0]._reason.isHidden()


class TestSettingsScrollbars:
    def test_the_panes_keep_the_apps_scrollbar(self, tmp_path):
        # The pane sets its own stylesheet, which would otherwise beat the
        # window's to its scrollbar.
        window = SettingsWindow(config_dir=tmp_path)

        style = window._panes.widget(0).styleSheet()

        assert "QScrollBar" in style
        assert "QScrollArea { background: transparent; }" in style

    def test_the_hide_list_text_boxes_keep_it_too(self, tmp_path):
        window = SettingsWindow(config_dir=tmp_path)

        style = window._hide_lists["words"].text.styleSheet()

        assert "QScrollBar" in style
        assert "QPlainTextEdit" in style


class TestAnnotationDefaults:
    """Settings -> Annotation: the default ink every coloured tool starts
    with, and per-tool defaults for anything a tool's style popover
    offers."""

    def _window(self, tmp_path):
        return SettingsWindow(config_dir=tmp_path)

    # -- default ink ---------------------------------------------------

    def test_the_default_ink_starts_as_each_tools_own(self, tmp_path):
        window = self._window(tmp_path)

        assert window._default_ink.value() is None
        assert window._default_ink.text() == "Each tool's own"

    def test_a_swatch_is_named_and_saved(self, tmp_path):
        window = self._window(tmp_path)
        window._default_ink.click()
        popup = window._default_ink.popup_widget()
        red = next(s for s in popup.swatches if s.colour == "#ef4444")

        red.click()

        assert window._default_ink.text() == "Red"
        assert window._dirty
        window._save()
        assert setup_desktop.load_default_ink(tmp_path) == "#ef4444"

    def test_any_colour_can_be_typed_as_hex(self, tmp_path):
        window = self._window(tmp_path)
        window._default_ink.click()
        popup = window._default_ink.popup_widget()

        popup.hex.setText("ff8800")
        popup.hex.returnPressed.emit()

        assert window._default_ink.value() == "#ff8800"
        assert window._default_ink.text() == "#FF8800"

    def test_a_hex_that_is_not_a_colour_changes_nothing(self, tmp_path):
        window = self._window(tmp_path)
        window._default_ink.click()
        popup = window._default_ink.popup_widget()

        popup.hex.setText("#ff88")
        popup.hex.returnPressed.emit()

        assert window._default_ink.value() is None
        assert not window._dirty

    def test_each_tools_own_can_be_chosen_back(self, tmp_path):
        setup_desktop.save_default_ink("#ef4444", tmp_path)
        window = self._window(tmp_path)
        window._default_ink.click()

        window._default_ink.popup_widget().inherit.click()
        window._save()

        assert setup_desktop.load_default_ink(tmp_path) is None

    # -- per-tool defaults ---------------------------------------------

    @staticmethod
    def _pick_tool(window, tool):
        window._custom_tool._choose(tool)

    def test_only_tools_with_something_to_set_are_offered(self, tmp_path):
        window = self._window(tmp_path)

        offered = window._custom_tool.values()

        assert offered == [t for t in tokens.TOOLS if tokens.STYLE_SECTIONS.get(t)]
        assert "blackout" not in offered and "eraser" not in offered

    @pytest.mark.parametrize("tool", ["pen", "rect", "blur", "highlighter", "arrow"])
    def test_a_tool_shows_the_rows_its_style_popover_has_and_no_others(self, tmp_path, tool):
        window = self._window(tmp_path)

        self._pick_tool(window, tool)

        shown = [
            section for section, row in window._tool_rows.items()
            if not row.isHidden()
        ]
        assert shown == [s for s in window._tool_rows if s in tokens.STYLE_SECTIONS[tool]]

    def test_a_tool_shows_what_it_ships_with_until_changed(self, tmp_path):
        window = self._window(tmp_path)

        self._pick_tool(window, "rect")

        assert window._tool_size.value() == tokens.DEFAULT_STYLE["rect"]["size"]
        assert window._tool_dash.value() == tokens.DEFAULT_STYLE["rect"]["dash"]
        assert window._tool_fill.value() == tokens.DEFAULT_STYLE["rect"]["fill"]
        assert window._tool_colour.value() is None

    def test_changes_are_kept_per_tool_and_saved_together(self, tmp_path):
        window = self._window(tmp_path)
        self._pick_tool(window, "rect")
        window._tool_dash._choose("dotted")
        window._tool_size.slider.setValue(9)
        self._pick_tool(window, "pen")
        window._tool_size.slider.setValue(12)
        self._pick_tool(window, "rect")

        # Back on the rectangle, its own changes are still what it shows.
        assert window._tool_dash.value() == "dotted"
        assert window._tool_size.value() == 9

        window._save()

        assert setup_desktop.load_tool_defaults(tmp_path) == {
            "rect": {"dash": "dotted", "size": 9},
            "pen": {"size": 12},
        }

    def test_setting_a_value_back_to_how_it_shipped_forgets_it(self, tmp_path):
        setup_desktop.save_tool_defaults({"rect": {"dash": "dotted"}}, tmp_path)
        window = self._window(tmp_path)
        self._pick_tool(window, "rect")

        window._tool_dash._choose(tokens.DEFAULT_STYLE["rect"]["dash"])
        window._save()

        assert setup_desktop.load_tool_defaults(tmp_path) == {}

    def test_a_tools_default_colour_follows_the_default_ink(self, tmp_path):
        window = self._window(tmp_path)
        self._pick_tool(window, "rect")
        window._default_ink.click()
        popup = window._default_ink.popup_widget()

        next(s for s in popup.swatches if s.colour == "#38bdf8").click()

        assert window._tool_colour._inherit_colours == ["#38bdf8"]

    def test_a_tool_can_keep_its_own_colour_under_a_default_ink(self, tmp_path):
        # Choosing the shipped colour explicitly is how one tool opts out.
        setup_desktop.save_default_ink("#38bdf8", tmp_path)
        window = self._window(tmp_path)
        self._pick_tool(window, "highlighter")
        window._tool_colour.click()

        next(
            s for s in window._tool_colour.popup_widget().swatches if s.colour == "#facc15"
        ).click()
        window._save()

        assert setup_desktop.load_tool_defaults(tmp_path) == {
            "highlighter": {"color": "#facc15"}
        }

    def test_reset_all_tools_clears_every_tools_defaults(self, tmp_path):
        setup_desktop.save_tool_defaults(
            {"rect": {"dash": "dotted"}, "pen": {"size": 12}}, tmp_path
        )
        window = self._window(tmp_path)
        self._pick_tool(window, "rect")

        window._reset_tools.click()

        assert window._tool_dash.value() == tokens.DEFAULT_STYLE["rect"]["dash"]
        window._save()
        assert setup_desktop.load_tool_defaults(tmp_path) == {}


class TestVersionLine:
    """`setup_desktop.version_line()`, what the nav rail's footer shows."""

    def test_is_snipuxs_own_version_and_nothing_else(self, monkeypatch):
        # It carried the Qt version and the session type too, which read as
        # noise: "we shouldnt even show the QT version just snipux version".
        monkeypatch.setattr(
            "importlib.metadata.version", lambda name: "1.2.3" if name == "snipux" else "9"
        )

        assert setup_desktop.version_line() == "Snipux 1.2.3"

    def test_the_footer_draws_no_border_of_its_own(self, tmp_path):
        # The rail's border rule used to reach the label and draw a hairline
        # down its right edge, inside the rail.
        window = SettingsWindow(config_dir=tmp_path)

        assert "border" not in window._version_label.styleSheet()
        assert window._version_label.parentWidget().objectName() == "navRail"


class TestTheWatermarkPage:
    """#69: what the stills bar's watermark stamps -- a line of text, or an
    image. Where it goes and how strongly are the bar's, per snip."""

    def _window(self, tmp_path, **kwargs):
        return SettingsWindow(config_dir=tmp_path, **kwargs)

    @staticmethod
    def _picture(tmp_path, name="logo.png", size=(64, 32)):
        image = QImage(*size, QImage.Format.Format_ARGB32)
        image.fill(QColor(255, 0, 255))
        path = tmp_path / "pictures" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        assert image.save(str(path))
        return path

    @staticmethod
    def _no_dialogs(monkeypatch) -> list:
        warnings = []
        monkeypatch.setattr(
            QMessageBox, "warning", lambda parent, title, text: warnings.append(text)
        )
        return warnings

    def test_the_rail_has_a_page_for_it_after_annotation(self, tmp_path):
        window = self._window(tmp_path)

        labels = [r.text().replace("&&", "&") for r in window._nav_group.buttons()]

        assert labels.index("Watermark") == labels.index("Annotation") + 1

    def test_its_row_opens_its_page(self, tmp_path):
        window = self._window(tmp_path)
        index = [identifier for identifier, _icon, _label in tokens.SETTINGS_NAV].index("watermark")

        window._nav_group.button(index).click()

        assert window._panes.currentWidget().widget().isAncestorOf(window._watermark_text)

    def test_every_other_row_still_opens_its_own_page(self, tmp_path):
        window = self._window(tmp_path)
        pages = {
            "capture": window._recorder,
            "saving": window._folder,
            "ink": window._show_hints,
            "hide": window._hide_file,
        }

        for index, (identifier, _icon, _label) in enumerate(tokens.SETTINGS_NAV):
            if identifier not in pages:
                continue
            window._nav_group.button(index).click()
            assert window._panes.currentWidget().widget().isAncestorOf(pages[identifier]), identifier

    def test_with_nothing_set_it_opens_on_text_and_no_image(self, tmp_path):
        window = self._window(tmp_path)

        assert window._watermark_cards["text"].isChecked()
        assert window._watermark_text.text() == ""
        assert window._watermark_thumb.text() == "No image"
        assert window._watermark_image_note.text().startswith("No image chosen yet")
        assert not window._watermark_remove.isEnabled()

    def test_it_opens_showing_what_is_stored(self, tmp_path):
        setup_desktop.save_watermark_kind("image", tmp_path)
        setup_desktop.save_watermark_text("acme", tmp_path)
        setup_desktop.save_watermark_image(self._picture(tmp_path), tmp_path)

        window = self._window(tmp_path)

        assert window._watermark_cards["image"].isChecked()
        assert window._watermark_text.text() == "acme"
        assert not window._watermark_thumb.pixmap().isNull()
        assert window._watermark_image_note.text().startswith("logo.png · 64 × 32 px")
        assert window._watermark_remove.isEnabled()
        assert not window._dirty

    def test_typing_a_mark_chooses_text_and_marks_the_window_dirty(self, tmp_path):
        setup_desktop.save_watermark_kind("image", tmp_path)
        window = self._window(tmp_path)

        QTest.keyClicks(window._watermark_text, "acme")

        assert window._watermark_cards["text"].isChecked()
        assert window._dirty

    def test_choosing_an_image_shows_it_and_chooses_image(self, tmp_path):
        window = self._window(tmp_path)

        window._choose_watermark_image(self._picture(tmp_path))

        assert window._watermark_cards["image"].isChecked()
        assert not window._watermark_thumb.pixmap().isNull()
        assert "Snipux keeps its own copy" in window._watermark_image_note.text()
        assert window._dirty

    def test_nothing_is_written_until_save(self, tmp_path):
        window = self._window(tmp_path)
        window._choose_watermark_image(self._picture(tmp_path))
        QTest.keyClicks(window._watermark_text, "acme")

        assert setup_desktop.load_watermark_image_name(tmp_path) is None
        assert setup_desktop.load_watermark_text(tmp_path) == ""

    def test_save_keeps_a_copy_of_the_chosen_image(self, tmp_path):
        source = self._picture(tmp_path)
        window = self._window(tmp_path)
        window._choose_watermark_image(source)

        window._save()

        kept = setup_desktop.load_watermark_image(tmp_path)
        assert kept is not None and kept != source
        assert kept.read_bytes() == source.read_bytes()
        assert setup_desktop.load_watermark_kind(tmp_path) == "image"

    def test_save_writes_the_kind_and_the_text(self, tmp_path):
        setup_desktop.save_watermark_kind("image", tmp_path)
        window = self._window(tmp_path)
        QTest.keyClicks(window._watermark_text, "acme · internal")

        window._save()

        assert setup_desktop.load_watermark_kind(tmp_path) == "text"
        assert setup_desktop.load_watermark_text(tmp_path) == "acme · internal"

    def test_a_file_that_is_not_an_image_is_refused_on_the_spot(self, tmp_path, monkeypatch):
        warnings = self._no_dialogs(monkeypatch)
        not_a_picture = tmp_path / "notes.png"
        not_a_picture.write_text("these are notes")
        window = self._window(tmp_path)

        window._choose_watermark_image(not_a_picture)

        assert len(warnings) == 1 and "notes.png" in warnings[0]
        assert window._watermark_cards["text"].isChecked()
        assert window._watermark_thumb.text() == "No image"
        assert not window._dirty

    def test_a_kept_image_that_went_missing_is_named_and_asked_for_again(self, tmp_path):
        setup_desktop.save_watermark_kind("image", tmp_path)
        setup_desktop.save_watermark_image(self._picture(tmp_path), tmp_path)
        setup_desktop.load_watermark_image(tmp_path).unlink()

        window = self._window(tmp_path)

        note = window._watermark_image_note.text()
        assert window._watermark_thumb.text() == "Missing"
        assert "logo.png" in note and "Choose the image again" in note
        assert window._watermark_remove.isEnabled()

    def test_remove_then_save_forgets_the_image(self, tmp_path):
        setup_desktop.save_watermark_image(self._picture(tmp_path), tmp_path)
        window = self._window(tmp_path)

        window._watermark_remove.click()

        assert window._watermark_thumb.text() == "No image"
        assert not window._watermark_remove.isEnabled()
        assert window._dirty
        window._save()
        assert setup_desktop.load_watermark_image_name(tmp_path) is None

    def test_it_opens_on_the_kept_colour_font_and_plate(self, tmp_path):
        setup_desktop.save_watermark_color("#ef4444", tmp_path)
        setup_desktop.save_watermark_font("Georgia", tmp_path)
        setup_desktop.save_watermark_backing(False, tmp_path)

        window = self._window(tmp_path)

        picked = [s for s in window._watermark_swatches if s.isChecked()]
        assert [s.color for s in picked] == ["#ef4444"]
        assert window._watermark_font.currentData() == "Georgia"
        assert window._watermark_backing.switch.isChecked() is False

    def test_by_default_it_is_the_plates_light_type_in_the_app_font(self, tmp_path):
        window = self._window(tmp_path)

        assert window._watermark_swatches[0].isChecked()
        assert window._watermark_font.currentIndex() == 0
        assert window._watermark_font.currentData() == ""
        assert window._watermark_backing.switch.isChecked()
        assert window._watermark_custom_swatch.isHidden()

    def test_a_swatch_font_and_plate_are_saved(self, tmp_path, monkeypatch):
        self._no_dialogs(monkeypatch)
        window = self._window(tmp_path)

        window._watermark_swatches[3].click()
        # Whatever this machine lists first after Default: CI's fonts are
        # not this desk's.
        family = window._watermark_font.itemData(1)
        window._watermark_font.setCurrentIndex(1)
        window._watermark_backing.switch.setChecked(False)
        assert "Unsaved changes" in window._dirty_label.text()
        window._save()

        assert setup_desktop.load_watermark_color(tmp_path) == tokens.WATERMARK_SWATCHES[3][1].lower()
        assert setup_desktop.load_watermark_font(tmp_path) == family
        assert setup_desktop.load_watermark_backing(tmp_path) is False

    def test_a_custom_colour_gets_its_own_swatch_and_is_saved(self, tmp_path, monkeypatch):
        self._no_dialogs(monkeypatch)
        window = self._window(tmp_path)

        window._choose_watermark_color(QColor(18, 52, 86))

        assert not window._watermark_custom_swatch.isHidden()
        assert window._watermark_custom_swatch.isChecked()
        assert not any(s.isChecked() for s in window._watermark_swatches)
        window._save()
        assert setup_desktop.load_watermark_color(tmp_path) == "#123456"

    def test_a_cancelled_colour_dialog_changes_nothing(self, tmp_path):
        window = self._window(tmp_path)

        window._choose_watermark_color(QColor())

        assert "Everything saved" in window._dirty_label.text()

    def test_styling_the_text_chooses_text(self, tmp_path):
        setup_desktop.save_watermark_kind("image", tmp_path)
        window = self._window(tmp_path)

        window._watermark_swatches[4].click()

        assert window._watermark_cards["text"].isChecked()

    def test_a_kept_font_this_machine_lacks_is_still_offered(self, tmp_path):
        setup_desktop.save_watermark_font("No Such Family 9", tmp_path)

        window = self._window(tmp_path)

        assert window._watermark_font.currentData() == "No Such Family 9"

    def test_the_preview_shows_the_chosen_colour(self, tmp_path):
        window = self._window(tmp_path)
        window._watermark_text.setText("ACME")

        window._choose_watermark_color(QColor(255, 0, 0))

        image = window._watermark_preview.pixmap().toImage()
        reds = sum(
            1
            for x in range(image.width())
            for y in range(image.height())
            if (c := image.pixelColor(x, y)).red() > 200 and c.green() < 60 and c.blue() < 60
        )
        assert reds > 10


class TestASaveThatCannotWrite:
    """Every `setup_desktop.save_*` returns False rather than raising when
    the write fails. Settings discarded all ~24 of those returns, then set
    itself clean and closed -- so a read-only config directory lost every
    setting and said "Everything saved" while doing it.
    """

    @staticmethod
    def _window(tmp_path, monkeypatch):
        window = SettingsWindow(config_dir=tmp_path)
        warned = []
        monkeypatch.setattr(
            settings_module.QMessageBox,
            "warning",
            lambda *args, **kwargs: warned.append(args[1:3]),
        )
        return window, warned

    def test_the_window_stays_open_and_dirty(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            settings_module.setup_desktop, "save_save_folder", lambda *a, **k: False
        )
        window, warned = self._window(tmp_path, monkeypatch)
        window._dirty = True

        window._save()

        assert window.isVisible() is False or not window.isHidden()
        assert window._dirty is True, "a failed save must not look saved"
        assert warned, "the user has to be told the write failed"

    def test_it_names_the_file_it_could_not_write(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            settings_module.setup_desktop, "save_tray_toggles", lambda *a, **k: False
        )
        window, warned = self._window(tmp_path, monkeypatch)

        window._save()

        assert warned
        assert "config.json" in warned[0][1]

    def test_a_save_that_works_still_closes_and_clears(self, tmp_path, monkeypatch):
        window, warned = self._window(tmp_path, monkeypatch)
        window._dirty = True

        window._save()

        assert not warned
        assert window._dirty is False


class TestTheCursorSwitchIsHonestAboutThePlatform:
    """It was a plain switch everywhere and only ever reached GNOME: on
    Windows it moved, saved, and changed nothing about the recording. The
    row's own flag (chooser.py) is greyed with a reason there, and this has
    to say the same thing or the two surfaces disagree.
    """

    def test_it_is_live_where_the_platform_can_honour_it(self, tmp_path, monkeypatch):
        monkeypatch.setattr(settings_module.platform.current, "records_cursor", lambda: True)

        window = SettingsWindow(config_dir=tmp_path)

        assert window._draw_cursor.switch.isEnabled()

    def test_it_is_greyed_where_it_cannot(self, tmp_path, monkeypatch):
        monkeypatch.setattr(settings_module.platform.current, "records_cursor", lambda: False)
        monkeypatch.setattr(
            settings_module.platform.current,
            "cursor_toggle_unavailable_reason",
            lambda: "Windows records whatever the capture shows",
        )

        window = SettingsWindow(config_dir=tmp_path)

        assert not window._draw_cursor.switch.isEnabled()
