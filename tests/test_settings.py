"""Tests for `snipux/settings.py` -- the Settings window and the key-press
-> GNOME accelerator conversion behind its recorder.
"""

from __future__ import annotations

import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QKeyEvent
from PyQt6.QtWidgets import QApplication

from snipux import setup_desktop
from snipux.settings import SettingsDialog, ShortcutRecorder, accelerator_from_event


@pytest.fixture(scope="module", autouse=True)
def qapp():
    # PyQt6 needs a live QApplication to construct any QWidget, even
    # offscreen. Module-scoped so every test in this file shares one --
    # same fixture test_overlay.py uses, for the same reason.
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def press(key: Qt.Key, modifiers=Qt.KeyboardModifier.NoModifier) -> QKeyEvent:
    return QKeyEvent(QKeyEvent.Type.KeyPress, key, modifiers)


class TestAcceleratorFromEvent:
    """Qt names keys its own way; gsettings stores them GNOME's way."""

    def test_modifiers_and_a_letter(self):
        event = press(
            Qt.Key.Key_X,
            Qt.KeyboardModifier.MetaModifier | Qt.KeyboardModifier.ShiftModifier,
        )

        assert accelerator_from_event(event) == "<Super><Shift>x"

    def test_letters_are_lowercased_the_way_gnome_writes_them(self):
        assert accelerator_from_event(press(Qt.Key.Key_S, Qt.KeyboardModifier.MetaModifier)) == (
            "<Super>s"
        )

    def test_named_keys_keep_their_capitalisation(self):
        assert accelerator_from_event(press(Qt.Key.Key_Print, Qt.KeyboardModifier.AltModifier)) == (
            "<Alt>Print"
        )

    def test_a_function_key_needs_no_modifier(self):
        assert accelerator_from_event(press(Qt.Key.Key_F9)) == "F9"

    def test_control_and_alt_map_to_gnome_tokens(self):
        event = press(
            Qt.Key.Key_P,
            Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier,
        )

        assert accelerator_from_event(event) == "<Control><Alt>p"

    @pytest.mark.parametrize(
        "key",
        [
            Qt.Key.Key_Shift,
            Qt.Key.Key_Control,
            Qt.Key.Key_Alt,
            Qt.Key.Key_Meta,
            Qt.Key.Key_Super_L,
        ],
    )
    def test_a_bare_modifier_is_not_a_shortcut_yet(self, key):
        # None means "keep listening" -- the first modifier going down must
        # not end the capture on a meaningless accelerator.
        assert accelerator_from_event(press(key)) is None

    def test_the_result_always_satisfies_the_cli_validator(self):
        # The dialog and `--setup --shortcut` must not disagree about what a
        # valid shortcut is.
        event = press(
            Qt.Key.Key_K,
            Qt.KeyboardModifier.MetaModifier | Qt.KeyboardModifier.ControlModifier,
        )

        accelerator = accelerator_from_event(event)

        assert setup_desktop.validate_shortcut(accelerator) is None


class TestShortcutRecorder:
    def test_shows_the_current_shortcut_in_human_form(self):
        recorder = ShortcutRecorder("<Super><Shift>x")

        assert recorder.text() == "Super+Shift+X"

    def test_recording_a_key_replaces_the_value_and_stops(self):
        recorder = ShortcutRecorder(setup_desktop.DEFAULT_SHORTCUT)
        recorded = []
        recorder.recorded.connect(recorded.append)
        recorder.setChecked(True)
        recorder._toggle_recording()

        recorder.keyPressEvent(press(Qt.Key.Key_J, Qt.KeyboardModifier.MetaModifier))

        assert recorded == ["<Super>j"]
        assert recorder.shortcut_value() == "<Super>j"
        assert not recorder.isChecked(), "recording should stop after one capture"

    def test_a_bare_modifier_does_not_end_recording(self):
        recorder = ShortcutRecorder(setup_desktop.DEFAULT_SHORTCUT)
        recorder.setChecked(True)
        recorder._toggle_recording()

        recorder.keyPressEvent(press(Qt.Key.Key_Shift))

        assert recorder.isChecked(), "still waiting for a real key"
        assert recorder.shortcut_value() == setup_desktop.DEFAULT_SHORTCUT

    def test_escape_cancels_without_changing_the_shortcut(self):
        # Someone who opened the recorder by accident needs a way out that
        # doesn't rebind anything.
        recorder = ShortcutRecorder("<Super><Shift>x")
        recorder.setChecked(True)
        recorder._toggle_recording()

        recorder.keyPressEvent(press(Qt.Key.Key_Escape))

        assert not recorder.isChecked()
        assert recorder.shortcut_value() == "<Super><Shift>x"

    def test_keys_are_ignored_entirely_while_not_recording(self):
        recorder = ShortcutRecorder("<Super><Shift>x")

        recorder.keyPressEvent(press(Qt.Key.Key_J, Qt.KeyboardModifier.MetaModifier))

        assert recorder.shortcut_value() == "<Super><Shift>x"


class TestSettingsDialog:
    def test_opens_showing_the_stored_shortcut(self, tmp_path):
        setup_desktop.save_shortcut("<Alt>Print", tmp_path)

        dialog = SettingsDialog(config_dir=tmp_path)

        assert dialog._recorder.shortcut_value() == "<Alt>Print"

    def test_the_review_window_is_off_unless_turned_on(self, tmp_path):
        dialog = SettingsDialog(config_dir=tmp_path)

        assert dialog._review_window.isChecked() is False

    def test_saving_persists_both_settings(self, tmp_path):
        dialog = SettingsDialog(config_dir=tmp_path)
        dialog._recorder.set_shortcut("<Super><Shift>k")
        dialog._review_window.setChecked(True)

        dialog._save()

        assert setup_desktop.load_shortcut(tmp_path) == "<Super><Shift>k"
        assert setup_desktop.load_review_window(tmp_path) is True

    def test_saving_calls_back_so_the_shortcut_can_be_rebound(self, tmp_path):
        # Remembering the shortcut is not binding it: GNOME only knows about
        # the binding it was told, and the callback is what tells it.
        called = []
        dialog = SettingsDialog(config_dir=tmp_path, on_saved=lambda: called.append(True))

        dialog._save()

        assert called == [True]

    def test_cancelling_persists_nothing(self, tmp_path):
        dialog = SettingsDialog(config_dir=tmp_path)
        dialog._recorder.set_shortcut("<Super><Shift>k")

        dialog.reject()

        assert setup_desktop.load_shortcut(tmp_path) == setup_desktop.DEFAULT_SHORTCUT

    def test_warns_when_gnome_already_uses_the_shortcut(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            setup_desktop,
            "find_shortcut_conflicts",
            lambda shortcut: [("org.gnome.shell.keybindings", "toggle-overview")],
        )

        dialog = SettingsDialog(config_dir=tmp_path)

        text = dialog._conflict_label.text()
        assert "toggle overview" in text
        # "may not work", never "will not": GNOME picks a winner between
        # duplicate bindings and does not say which.
        assert "may not work" in text

    def test_says_nothing_stronger_than_no_gnome_shortcut_uses_it(
        self, tmp_path, monkeypatch
    ):
        # An app that grabs a key directly is invisible to the check, so the
        # dialog must never claim a key is free.
        monkeypatch.setattr(setup_desktop, "find_shortcut_conflicts", lambda shortcut: [])

        dialog = SettingsDialog(config_dir=tmp_path)

        assert dialog._conflict_label.text() == "No GNOME shortcut uses this."
