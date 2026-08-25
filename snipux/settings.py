"""The Settings window: a GUI for the things `snipux --setup` takes on the
command line.

`setup_desktop.py` is where the shortcut is validated, stored and bound, and
it deliberately imports no Qt at all -- `--setup` runs with no display and
must keep working that way. This module is the other half: the Qt in front
of it. Everything here reads and writes through `setup_desktop`'s own
functions rather than touching `config.json` or `gsettings` itself, so the
CLI and the dialog can never disagree about what a valid shortcut is or
where it is remembered.

The one piece of real logic here is `accelerator_from_event` -- turning a
key press into GNOME's accelerator syntax -- which has to live on this side
because only Qt knows what was pressed.
"""

from __future__ import annotations

from typing import Callable

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QKeyEvent, QKeySequence
from PyQt6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from . import setup_desktop

# Qt modifier -> GNOME accelerator token. <Super> rather than <Meta>, and
# <Control> rather than <Primary>: both spellings are accepted by gsettings,
# but these are the ones GNOME itself writes, so a binding set here reads
# identically to one set in Settings.
#
# The order matters only for how the result *reads*: gtk_accelerator_parse
# accepts modifiers in any order, so nothing breaks either way. This one
# reproduces what GNOME already has on disk -- '<Super><Control>1' in
# org.gnome.shell.keybindings, and this project's own '<Super><Shift>s'
# default -- so a shortcut recorded here is byte-identical to the same
# combination bound through GNOME Settings, rather than a permutation of it
# that only looks different.
_MODIFIER_TOKENS = (
    (Qt.KeyboardModifier.MetaModifier, "<Super>"),
    (Qt.KeyboardModifier.ControlModifier, "<Control>"),
    (Qt.KeyboardModifier.AltModifier, "<Alt>"),
    (Qt.KeyboardModifier.ShiftModifier, "<Shift>"),
)

# Pressed alone these are not a shortcut, they are the user still reaching
# for one. Recording has to ignore them or the first modifier down would end
# the capture with a meaningless accelerator.
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
    """A key press -> GNOME accelerator syntax, or None if it isn't one yet.

    None means "keep listening": a bare modifier, or a key Qt cannot name.
    Everything else comes back in the form gsettings stores --
    `<Super><Shift>x`, `<Alt>Print` -- with single letters lowercased, the
    way GNOME writes them.
    """
    key = event.key()
    if key in _BARE_MODIFIER_KEYS or key == Qt.Key.Key_unknown:
        return None

    name = QKeySequence(key).toString()
    if not name:
        return None
    # Qt renders letters uppercase ("S"); GNOME stores them lowercase. Named
    # keys ("Print", "F9") keep their own capitalisation.
    if len(name) == 1:
        name = name.lower()

    modifiers = event.modifiers()
    tokens = [token for flag, token in _MODIFIER_TOKENS if modifiers & flag]
    accelerator = "".join(tokens) + name

    # Validated here rather than trusted: Qt will happily name keys that are
    # not accelerators, and the same rule the CLI applies should apply to a
    # recorded one.
    if setup_desktop.validate_shortcut(accelerator) is not None:
        return None
    return accelerator


class ShortcutRecorder(QPushButton):
    """A button that, once clicked, becomes the next key press.

    A plain text field would mean typing `<Super><Shift>x` by hand, which is
    the syntax this dialog exists to hide.
    """

    recorded = pyqtSignal(str)

    def __init__(self, shortcut: str, parent: QWidget | None = None):
        super().__init__(parent)
        self._shortcut = shortcut
        self._recording = False
        self.setCheckable(True)
        self.clicked.connect(self._toggle_recording)
        self._refresh()

    def shortcut_value(self) -> str:
        return self._shortcut

    def set_shortcut(self, shortcut: str) -> None:
        self._shortcut = shortcut
        self._refresh()

    def _refresh(self) -> None:
        if self._recording:
            self.setText("Press a key combination…  (Esc to cancel)")
        else:
            self.setText(setup_desktop.human_shortcut(self._shortcut))

    def _toggle_recording(self) -> None:
        self._recording = self.isChecked()
        if self._recording:
            self.grabKeyboard()
        else:
            self.releaseKeyboard()
        self._refresh()

    def _stop_recording(self) -> None:
        self._recording = False
        self.setChecked(False)
        self.releaseKeyboard()
        self._refresh()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if not self._recording:
            super().keyPressEvent(event)
            return
        # Esc cancels rather than binding: a user who opened the recorder by
        # accident needs a way out that doesn't change their shortcut, and
        # Esc is nobody's idea of a snip key.
        if event.key() == Qt.Key.Key_Escape and not event.modifiers():
            self._stop_recording()
            return
        accelerator = accelerator_from_event(event)
        if accelerator is None:
            # Still a modifier, or something unnameable -- keep listening
            # rather than ending the capture on it.
            return
        self._shortcut = accelerator
        self._stop_recording()
        self.recorded.emit(accelerator)


class SettingsDialog(QDialog):
    """Settings, reachable from the tray. Currently the shortcut and the
    post-capture review window.

    `on_saved` is called with no arguments once changes are applied, so the
    controller that owns the tray can act on them (rebinding the shortcut,
    picking up the new preference) without this dialog knowing what a
    controller is.
    """

    def __init__(
        self,
        *,
        parent: QWidget | None = None,
        config_dir=None,
        on_saved: Callable[[], None] | None = None,
    ):
        super().__init__(parent)
        self._config_dir = config_dir
        self._on_saved = on_saved

        self.setWindowTitle("snipux Settings")
        self.setModal(False)

        layout = QVBoxLayout(self)
        form = QFormLayout()

        self._recorder = ShortcutRecorder(setup_desktop.load_shortcut(config_dir))
        self._recorder.recorded.connect(self._on_recorded)
        form.addRow("Shortcut", self._recorder)

        # Sits under the recorder rather than in a message box: a conflict is
        # a warning about a choice being made right now, not an error that
        # should interrupt making it.
        self._conflict_label = QLabel()
        self._conflict_label.setWordWrap(True)
        form.addRow("", self._conflict_label)

        self._review_window = QCheckBox("Open each snip in a review window")
        self._review_window.setChecked(setup_desktop.load_review_window(config_dir))
        form.addRow("After capture", self._review_window)

        layout.addLayout(form)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        row = QHBoxLayout()
        row.addWidget(buttons)
        layout.addLayout(row)

        self._refresh_conflicts()

    def _on_recorded(self, _accelerator: str) -> None:
        self._refresh_conflicts()

    def _refresh_conflicts(self) -> None:
        shortcut = self._recorder.shortcut_value()
        description = setup_desktop.describe_conflicts(
            setup_desktop.find_shortcut_conflicts(shortcut)
        )
        if description:
            # "may not work" rather than "will not": GNOME picks a winner
            # between duplicate bindings and does not say which.
            self._conflict_label.setText(f"⚠ {description} — it may not work.")
        else:
            # Deliberately not "this key is free": an application that grabs
            # a key directly, rather than through GNOME, is invisible to
            # find_shortcut_conflicts and owns it just as effectively.
            self._conflict_label.setText("No GNOME shortcut uses this.")

    def _save(self) -> None:
        shortcut = self._recorder.shortcut_value()
        setup_desktop.save_shortcut(shortcut, self._config_dir)
        setup_desktop.save_review_window(
            self._review_window.isChecked(), self._config_dir
        )
        if self._on_saved is not None:
            self._on_saved()
        self.accept()
