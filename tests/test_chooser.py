"""The stills/record switch, SNX-120 / docs/design/recording.md ticket 5.

UI and state only -- nothing here is wired to a recorder, and nothing in
this file should call into `recording.py` or any platform registry. What it
covers: the switch itself, `Chooser.kind` and the mode/after snapping that
follows it, the record side's narrowed mode list and "then" vocabulary, and
that a disabled mode row is inert rather than merely greyed.

Everything that needs a hosting `OverlayWindow` (press-swallowing via
`TestTheChooserTakesItsOwnClicks`) stays in test_overlay.py, per that
class's own fixture -- this file constructs `Chooser(parent=None)` directly,
since none of the above needs the overlay underneath it.
"""

import pytest
from PyQt6.QtCore import QPoint, Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication

from snipux.chooser import _AFTER_ROWS, _MenuRow, _RECORD_AFTER_ROWS, Chooser
from snipux.design import tokens


@pytest.fixture(scope="module", autouse=True)
def qapp():
    # PyQt6 needs a live QApplication to construct any QWidget, even
    # offscreen -- matching the convention in test_overlay.py/test_design.py.
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def _centre(widget):
    return QPoint(widget.width() // 2, widget.height() // 2)


def _click(widget):
    QTest.mouseClick(widget, Qt.MouseButton.LeftButton, pos=_centre(widget))


class TestTheKindDefaultsToStills:
    def test_a_fresh_chooser_starts_on_stills(self):
        chooser = Chooser(parent=None)

        assert chooser.kind == "stills"


class TestClickingTheSwitchTogglesKind:
    def test_the_record_side_opens_on_the_configured_destination(self):
        # Recording's destination is a Settings row now. The chooser used
        # to reset to tokens.RECORD_AFTER_DEFAULT on every switch to the
        # record side, so a stored preference had nowhere to take effect.
        chooser = Chooser(parent=None)
        chooser.set_record_after_default("save")

        chooser.set_kind("record")

        assert chooser.after == "save"

    def test_the_record_default_applies_immediately_when_already_recording(self):
        chooser = Chooser(parent=None)
        chooser.set_kind("record")

        chooser.set_record_after_default("save")

        assert chooser.after == "save"

    def test_a_stills_only_destination_is_refused_as_a_record_default(self):
        # "review" is a stills id; nothing downstream of a recording knows
        # what to do with it, so it must not become the record side's seed.
        chooser = Chooser(parent=None)

        chooser.set_record_after_default("review")
        chooser.set_kind("record")

        assert chooser.after == tokens.RECORD_AFTER_DEFAULT

    def test_it_flips_to_record(self):
        chooser = Chooser(parent=None)

        _click(chooser.panel.kind_switch)

        assert chooser.kind == "record"

    def test_it_flips_back_to_stills(self):
        chooser = Chooser(parent=None)
        chooser.set_kind("record")

        _click(chooser.panel.kind_switch)

        assert chooser.kind == "stills"

    def test_it_changes_only_kind_when_the_current_selection_still_fits(self):
        # Region/instant are valid on both sides, so flipping the switch
        # with them already selected must not touch phase, mode or after --
        # only the axis that was actually clicked.
        chooser = Chooser(parent=None)
        mode_chosen = []
        fired = []
        chooser.modeChosen.connect(mode_chosen.append)
        chooser.fireImmediately.connect(fired.append)

        _click(chooser.panel.kind_switch)

        assert chooser.kind == "record"
        assert chooser.phase == "choosing"
        assert chooser.mode == "Region"
        assert chooser.after == "instant"
        assert mode_chosen == []
        assert fired == []


class TestKindPersistsAcrossReopen:
    def test_reopen_leaves_kind_alone(self):
        chooser = Chooser(parent=None)
        chooser.set_kind("record")
        chooser.set_mode("Region")  # arms it, same as any other mode pick

        chooser.reopen()

        assert chooser.kind == "record"
        assert chooser.phase == "choosing"


class TestKindChangedSignal:
    """`kindChanged` is what lets something outside the chooser persist the
    switch across separate snips (see `overlay.py`'s wiring to
    `setup_desktop.save_kind`) -- unlike `after`/`delay`, `kind` has no
    Settings surface, so the chooser itself has to announce every real
    flip.
    """

    def test_flipping_the_switch_emits_the_new_kind(self):
        chooser = Chooser(parent=None)
        emitted = []
        chooser.kindChanged.connect(emitted.append)

        chooser.set_kind("record")

        assert emitted == ["record"]

    def test_setting_the_same_kind_again_emits_nothing(self):
        # `set_kind` already no-ops on a same-value call (see its early
        # return above); a signal here would make overlay.py re-save a
        # value that never changed.
        chooser = Chooser(parent=None)
        emitted = []
        chooser.kindChanged.connect(emitted.append)

        chooser.set_kind("stills")

        assert emitted == []

    def test_an_invalid_kind_emits_nothing(self):
        chooser = Chooser(parent=None)
        emitted = []
        chooser.kindChanged.connect(emitted.append)

        chooser.set_kind("nonsense")

        assert emitted == []


class TestSwitchingToRecordSnapsAnUnavailableMode:
    @pytest.mark.parametrize("mode", ["Window", "Freeform"])
    def test_it_snaps_to_region(self, mode):
        chooser = Chooser(parent=None)
        chooser.set_mode(mode, arm=False)

        chooser.set_kind("record")

        assert chooser.mode == "Region"

    def test_a_mode_already_valid_on_the_record_side_is_left_alone(self):
        chooser = Chooser(parent=None)
        chooser.set_mode("Full screen", arm=False)

        chooser.set_kind("record")

        assert chooser.mode == "Full screen"

    def test_switching_back_to_stills_needs_no_snap(self):
        # The stills side's mode/after lists are the original, unrestricted
        # ones, so nothing there can ever be invalid.
        chooser = Chooser(parent=None)
        chooser.set_mode("Window", arm=False)
        chooser.set_kind("record")
        assert chooser.mode == "Region"

        chooser.set_kind("stills")

        assert chooser.mode == "Region"


class TestSwitchingToRecordSnapsAnUnavailableAfter:
    @pytest.mark.parametrize("after", ["edit", "review"])
    def test_it_snaps_to_instant(self, after):
        chooser = Chooser(parent=None)
        chooser.set_after(after)

        chooser.set_kind("record")

        assert chooser.after == "instant"

    def test_an_after_already_valid_on_the_record_side_is_left_alone(self):
        chooser = Chooser(parent=None)
        chooser.set_after("instant")

        chooser.set_kind("record")

        assert chooser.after == "instant"


class TestFullScreenBehavesDifferentlyPerKind:
    def test_stills_still_fires_immediately(self):
        chooser = Chooser(parent=None)
        fired = []
        chooser.fireImmediately.connect(fired.append)

        chooser.set_mode("Full screen")

        assert fired == ["Full screen"]
        assert chooser.phase == "choosing"

    def test_record_arms_instead(self):
        # Nothing downstream of the chooser knows about `kind` yet, so
        # firing here would silently run the existing stills-capture path
        # instead of doing nothing -- it must arm and wait like Region does.
        chooser = Chooser(parent=None)
        chooser.set_kind("record")
        fired = []
        chooser.fireImmediately.connect(fired.append)

        chooser.set_mode("Full screen")

        assert fired == []
        assert chooser.phase == "armed"


class TestRecordSideModeSelectionIsInert:
    @pytest.mark.parametrize("mode", ["Window", "Freeform"])
    def test_picking_a_disabled_mode_leaves_it_unchanged(self, mode):
        chooser = Chooser(parent=None)
        chooser.set_kind("record")

        chooser.set_mode(mode)

        assert chooser.mode == "Region"
        assert chooser.phase == "choosing"

    @pytest.mark.parametrize("key,mode", [("W", "Window"), ("L", "Freeform")])
    def test_the_shortcut_key_is_inert_too(self, key, mode):
        chooser = Chooser(parent=None)
        chooser.set_kind("record")

        chooser.handle_key(ord(key), key)

        assert chooser.mode == "Region"


class TestTheModeMenuNarrowsOnTheRecordSide:
    def test_stills_offers_all_four_modes_none_disabled(self):
        chooser = Chooser(parent=None)

        rows, _selected, _width = chooser._rows_for("mode")

        assert [row[0] for row in rows] == [m[0] for m in tokens.CAPTURE_MODES]
        assert all(row[5] is False for row in rows)

    def test_record_disables_window_and_freeform_with_a_note(self):
        chooser = Chooser(parent=None)
        chooser.set_kind("record")

        rows, _selected, _width = chooser._rows_for("mode")
        by_value = {value: (note, disabled) for value, _i, _l, note, _s, disabled in rows}

        for mode, reason in tokens.RECORD_DISABLED_MODES.items():
            note, disabled = by_value[mode]
            assert disabled is True
            assert note == reason

    def test_record_leaves_region_and_full_screen_enabled(self):
        chooser = Chooser(parent=None)
        chooser.set_kind("record")

        rows, _selected, _width = chooser._rows_for("mode")
        by_value = {value: disabled for value, _i, _l, _n, _s, disabled in rows}

        assert by_value["Region"] is False
        assert by_value["Full screen"] is False


class TestTheAfterMenuSwapsVocabularyOnTheRecordSide:
    def test_stills_is_unchanged_instant_edit_review(self):
        chooser = Chooser(parent=None)

        rows, _selected, _width = chooser._rows_for("after")

        assert [row[0] for row in rows] == ["instant", "edit", "review"]
        assert all(row[5] is False for row in rows)

    def test_record_offers_exactly_instant_and_save(self):
        chooser = Chooser(parent=None)
        chooser.set_kind("record")

        rows, _selected, _width = chooser._rows_for("after")

        assert [row[0] for row in rows] == ["instant", "save"]
        assert all(row[5] is False for row in rows)

    def test_edit_review_and_trim_never_appear_on_the_record_side(self):
        record_ids = {value for value, *_rest in _RECORD_AFTER_ROWS}

        assert "edit" not in record_ids
        assert "review" not in record_ids
        assert "trim" not in record_ids

    def test_save_does_not_exist_on_the_stills_side(self):
        # `AFTER_CAPTURE`/`_AFTER_ROWS` are stills-only -- "save" is not a
        # destination stills can pick, and must not leak into that list.
        stills_ids = {value for value, *_rest in _AFTER_ROWS}

        assert "save" not in stills_ids
        assert "save" not in {v for v, *_r in tokens.AFTER_CAPTURE}


class TestADisabledModeRowIsInertNotJustGreyed:
    def test_it_swallows_its_press_but_never_clicks(self):
        # SNX-108's fix (`_Surface`) still applies to a disabled row -- the
        # press must not reach whatever is behind the menu -- but the
        # release must not emit either, unlike an enabled row's.
        row = _MenuRow(
            "Window", "window", "Window", "Not offered for recording yet", "W",
            disabled=True,
        )
        row.resize(250, 40)
        clicked = []
        row.clicked.connect(clicked.append)

        _click(row)

        assert clicked == []

    def test_an_enabled_row_still_clicks_for_comparison(self):
        row = _MenuRow("Region", "crop", "Region", "", "R", disabled=False)
        row.resize(250, 40)
        clicked = []
        row.clicked.connect(clicked.append)

        _click(row)

        assert clicked == ["Region"]


def _darkest_pixel(pixmap, x0, x1, y0, y1):
    # `_MenuRow` is grabbed standalone here, with no hosting `_Menu` behind
    # it to paint `ChooserColor.MENU_BG` -- so the untouched background is
    # whatever plain Qt hands a fresh widget (a light grey), brighter than
    # any foreground colour this row ever paints. Against that background,
    # the shortcut glyph's own ink is what pulls a pixel's R+G+B *down* --
    # the most-inked pixel is therefore the darkest one, not the brightest.
    image = pixmap.toImage()
    return min(
        image.pixelColor(x, y).red() + image.pixelColor(x, y).green() + image.pixelColor(x, y).blue()
        for x in range(x0, x1)
        for y in range(y0, y1)
    )


class TestADisabledModeRowDimsItsShortcutToo:
    """SNX-120 review: `_rows_for("mode")` still handed a disabled row its
    shortcut letter, and `_MenuRow.paintEvent` drew it in `SHORTCUT_FG`
    regardless -- full brightness, as if the key still did something, when
    `handle_key` now silently no-ops for it on the record side
    (`TestRecordSideModeSelectionIsInert`). It must still be visible --
    "grey out with a hint", not hide -- just dimmed like the label/icon.
    """

    def test_a_disabled_rows_shortcut_is_dimmer_than_an_enabled_ones(self):
        # Both rows carry a note, so both land on the same fixed height
        # (`_MenuRow.__init__`'s `34 if note else 18`) -- resize() cannot
        # widen it past that, since setFixedHeight caps it even for a
        # parentless widget.
        enabled = _MenuRow("Full screen", "monitor", "Full screen", "note", "F", disabled=False)
        disabled = _MenuRow(
            "Window", "window", "Window", "Not offered for recording yet", "W",
            disabled=True,
        )
        enabled.resize(250, enabled.height())
        disabled.resize(250, disabled.height())
        assert enabled.height() == disabled.height()
        # The shortcut is drawn right-aligned in a 20px-wide slot inset by
        # MENU_ROW_PAD_H (9) from the row's right edge -- see `paintEvent`.
        x0, x1 = 250 - 9 - 20, 250 - 9

        enabled_ink = _darkest_pixel(enabled.grab(), x0, x1, 0, enabled.height())
        disabled_ink = _darkest_pixel(disabled.grab(), x0, x1, 0, disabled.height())

        assert disabled_ink < enabled_ink
