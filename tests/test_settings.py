"""Tests for `snipux/settings.py` -- the Settings window from
`docs/design/handoff-windows.md` section 2.
"""

from __future__ import annotations

import re

import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QKeyEvent
from PyQt6.QtWidgets import QAbstractButton, QApplication, QLabel, QMessageBox

from snipux import platform, setup_desktop
from snipux.design import tokens
from snipux.settings import (
    ConflictBanner,
    HotkeyEventFilter,
    SettingsWindow,
    ShortcutRecorder,
    accelerator_from_event,
)

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

    def test_save_commits_every_pane(self, tmp_path):
        window = self._window(tmp_path)
        window._recorder.set_shortcut("Control+Alt+K")
        window._after_group.buttons()[0].setChecked(True)  # "review"
        window._also_copy.switch.setChecked(True)
        window._filename.setText("shot-%Y")
        window._native.switch.setChecked(True)
        window._show_hints.switch.setChecked(True)

        window._save()

        assert setup_desktop.load_shortcut(tmp_path) == "Control+Alt+K"
        assert setup_desktop.load_after_capture(tmp_path) == "review"
        assert setup_desktop.load_always_copy(tmp_path) is True
        assert setup_desktop.load_filename_pattern(tmp_path) == "shot-%Y"
        assert setup_desktop.load_native_resolution(tmp_path) is True
        assert setup_desktop.load_hints_enabled(tmp_path) is True

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
        # Proves wrapping is load-bearing here, not just set and unused:
        # the text is wider than the width the rail actually granted the
        # label, so without word wrap this exact case would still clip.
        single_line_width = label.fontMetrics().horizontalAdvance(label.text())
        assert single_line_width > label.width()

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
        window._after_group.buttons()[0].setChecked(True)  # "review"

        window._save()

        assert setup_desktop.load_review_window(tmp_path) is True

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


class TestVersionLine:
    """`setup_desktop.version_line()`'s trailing field -- what
    `TestSettingsWindow`'s footer test above renders, tested here without a
    `SettingsWindow` in the way.
    """

    def test_shows_the_session_type_on_linux(self, monkeypatch):
        monkeypatch.setattr(setup_desktop.sys, "platform", "linux")
        monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
        monkeypatch.delenv("DISPLAY", raising=False)
        monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")

        assert setup_desktop.version_line().endswith("wayland")

    def test_shows_a_platform_name_on_windows_rather_than_an_always_unknown_session_type(
        self, monkeypatch
    ):
        # AC: Windows has no session-type concept, so the field must not be
        # the "unknown" `detect_session_type()` would always report there.
        monkeypatch.setattr(setup_desktop.sys, "platform", "win32")

        line = setup_desktop.version_line()

        assert line.endswith("Windows")
        assert "unknown" not in line

    def test_shows_a_platform_name_on_macos(self, monkeypatch):
        monkeypatch.setattr(setup_desktop.sys, "platform", "darwin")

        assert setup_desktop.version_line().endswith("macOS")
