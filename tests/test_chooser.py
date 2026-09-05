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
    # Window came off this list once recording gained it -- it was only
    # ever disabled because nobody had asked, and it resolves to a rect
    # like any region. Freeform stays: video is rectangular.
    @pytest.mark.parametrize("mode", ["Freeform"])
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
        chooser.set_mode("Freeform", arm=False)
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
    @pytest.mark.parametrize("mode", ["Freeform"])
    def test_picking_a_disabled_mode_leaves_it_unchanged(self, mode):
        chooser = Chooser(parent=None)
        chooser.set_kind("record")

        chooser.set_mode(mode)

        assert chooser.mode == "Region"
        assert chooser.phase == "choosing"

    @pytest.mark.parametrize("key,mode", [("L", "Freeform")])
    def test_the_shortcut_key_is_inert_too(self, key, mode):
        chooser = Chooser(parent=None)
        chooser.set_kind("record")

        chooser.handle_key(ord(key), key)

        assert chooser.mode == "Region"

    def test_window_is_live_on_the_record_side_now(self):
        # It was disabled for one reason -- nobody had asked -- and it
        # resolves to a rect exactly like a dragged region does, which is
        # all the recorder ever wanted.
        chooser = Chooser(parent=None)
        chooser.set_kind("record")

        chooser.set_mode("Window")

        assert chooser.mode == "Window"


class TestTheRowsTriggersAreReadable:
    """The handoff made mode the only labelled trigger -- destination "icon
    only ... secondary decision", delay's "label appears only when set".

    Destination no longer follows that. The rule assumed the glyph would
    carry the meaning and it does not: this control decides whether a snip
    ends with a toolbar, a window, or nothing at all, and a pen glyph
    versus an eye glyph does not say which. Icon-only, it was why a
    destination of `instant` could not be got out of -- that one ends the
    snip on the release of the drag, leaving no toolbar to notice it from.

    Delay keeps the handoff's rule, because a delay of Off is genuinely
    nothing to say.
    """

    def test_mode_keeps_its_label(self):
        chooser = Chooser(parent=None)

        assert chooser.panel.mode_trigger._label == "Region"

    def test_the_destination_trigger_says_which_destination(self):
        chooser = Chooser(parent=None)

        chooser.set_after("review")

        assert chooser.panel.after_trigger._label == "Review"
        assert chooser.after == "review"

    def test_every_destination_names_itself_on_both_sides(self):
        # Whatever is showing, the row has to be able to say what it is --
        # including the record side, whose vocabulary is different.
        for kind, rows in (("stills", _AFTER_ROWS), ("record", _RECORD_AFTER_ROWS)):
            chooser = Chooser(parent=None)
            chooser.set_kind(kind)
            for value, _icon, label, _note in rows:
                chooser.set_after(value)
                assert chooser.panel.after_trigger._label == label, (
                    f"{value} on the {kind} side"
                )

    def test_instant_is_flagged_the_way_an_armed_delay_is(self):
        # It is the one destination that can surprise you -- it ends the
        # snip the moment the drag is released -- so it is visible before
        # it does that rather than afterwards.
        chooser = Chooser(parent=None)

        chooser.set_after("instant")
        flagged = chooser.panel.after_trigger._label_colour

        chooser.set_after("edit")

        assert flagged == tokens.ChooserColor.MODE_ACCENT
        assert chooser.panel.after_trigger._label_colour != flagged

    def test_delay_is_bare_until_one_is_set(self):
        chooser = Chooser(parent=None)
        assert chooser.panel.delay_trigger._label == ""

        chooser.set_delay("5s")
        assert chooser.panel.delay_trigger._label == "5s"

        chooser.set_delay(tokens.DELAY_DEFAULT)
        assert chooser.panel.delay_trigger._label == ""

    def test_an_unlabelled_trigger_is_narrower_than_a_labelled_one(self):
        # The point of the change: the row was wide enough to read as a
        # toolbar rather than a sentence.
        chooser = Chooser(parent=None)
        bare = chooser.panel.delay_trigger.width()

        chooser.set_delay("10s")

        assert chooser.panel.delay_trigger.width() > bare


class TestEveryModeRowSaysWhatItCaptures:
    """Window and Full screen read as the same thing until you have used
    both -- "if you're capturing a window, you're capturing a full screen?"
    One is an application's window, the other a whole monitor, and the note
    is the only thing that distinguishes them at the moment of choosing.
    The menu used to show a note only for *disabled* rows, so every mode a
    user could actually pick explained nothing.
    """

    def _notes(self, kind):
        chooser = Chooser(parent=None)
        chooser.set_kind(kind)
        rows, _selected, _width = chooser._rows_for("mode")
        return {row[0]: row[3] for row in rows}

    def test_every_enabled_mode_carries_a_note(self):
        for kind in ("stills", "record"):
            for mode, note in self._notes(kind).items():
                assert note, f"{mode} on the {kind} side has no note"

    def test_window_and_full_screen_do_not_describe_the_same_thing(self):
        notes = self._notes("stills")

        assert "window" in notes["Window"].lower()
        assert "monitor" in notes["Full screen"].lower()
        assert notes["Window"] != notes["Full screen"]

    def test_the_record_side_says_a_window_is_filmed_where_it_is(self):
        # The recorder is handed a rectangle once and does not follow the
        # window afterwards, which is the surprise worth naming up front.
        assert "now" in self._notes("record")["Window"].lower()

    def test_a_disabled_rows_reason_outranks_its_description(self):
        notes = self._notes("record")

        assert notes["Freeform"] == tokens.RECORD_DISABLED_MODES["Freeform"]

    def test_no_note_is_long_enough_to_be_elided(self):
        # A note cut off mid-sentence is worse than none at all, and the
        # menu is a fixed width -- so this is measured, not eyeballed.
        from PyQt6.QtGui import QFontMetricsF
        from snipux.chooser import _font

        budget = tokens.ChooserMetric.MENU_MODE_W - 84  # icon, padding, shortcut
        metrics = QFontMetricsF(_font(11, 400))
        for kind in ("stills", "record"):
            for mode, note in self._notes(kind).items():
                assert metrics.horizontalAdvance(note) <= budget, (
                    f"{mode} on the {kind} side would elide: {note!r}"
                )


class TestTheModeMenuNarrowsOnTheRecordSide:
    def test_stills_offers_every_mode_and_disables_none_of_them(self):
        chooser = Chooser(parent=None)
        # Seeded, or `Tab` is the one row the stills side does grey out --
        # see TestTheTabModeNeedsABrowser, which is about that rule itself.
        chooser.set_browser_available(True)

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
    def test_stills_offers_instant_edit_save_and_review(self):
        chooser = Chooser(parent=None)

        rows, _selected, _width = chooser._rows_for("after")

        assert [row[0] for row in rows] == ["instant", "edit", "save", "review"]
        assert all(row[5] is False for row in rows)

    def test_record_offers_copy_save_and_open(self):
        chooser = Chooser(parent=None)
        chooser.set_kind("record")

        rows, _selected, _width = chooser._rows_for("after")

        assert [row[0] for row in rows] == ["instant", "save", "open"]
        assert all(row[5] is False for row in rows)

    def test_edit_and_review_never_appear_on_the_record_side(self):
        # "open" is the record side's third destination and means the trim
        # editor, not the stills review window -- the two are different
        # windows and the ids stay separate.
        record_ids = {value for value, *_rest in _RECORD_AFTER_ROWS}

        assert "edit" not in record_ids
        assert "review" not in record_ids

    def test_save_is_a_stills_destination_too_now(self):
        # It did not used to be, and the split action was the thing that
        # made that untenable: its caret has always offered Copy/Save/Open
        # and `_sync_bar_destination` has always mapped `save` to a Save
        # face, so with no stills id to record it in, picking Save from the
        # caret was the one choice that could not be remembered.
        stills_ids = {value for value, *_rest in _AFTER_ROWS}

        assert "save" in stills_ids
        assert "save" in {v for v, *_r in tokens.AFTER_CAPTURE}

    def test_the_two_lists_agree_on_what_the_shared_ids_mean(self):
        # `instant` and `save` appear on both sides and mean the same
        # thing by both; a divergence here would make `set_kind`'s snap
        # (which keeps `_after` when the new side also has it) silently
        # change what the user asked for.
        stills = {value for value, *_rest in _AFTER_ROWS}
        record = {value for value, *_rest in _RECORD_AFTER_ROWS}

        assert stills & record == {"instant", "save"}

    def test_stills_ids_still_round_trip_through_storage(self):
        # `load_after_capture` validates against `tokens.AFTER_CAPTURE`, so
        # a row the menu offers but storage rejects would silently fall
        # back to the default the moment it was read back.
        from snipux import setup_desktop

        for identifier, *_rest in _AFTER_ROWS:
            setup_desktop.save_after_capture(identifier)
            assert setup_desktop.load_after_capture() == identifier


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
    #
    # The window is given in LOGICAL pixels -- the space `_MenuRow.paintEvent`
    # draws in and the space its 9px/20px shortcut slot is expressed in -- but
    # `grab()` hands back PHYSICAL ones, so on a 1.5x display this row's 250
    # logical px are 375 in the image. Scaling the window here rather than at
    # the call sites keeps every caller in the one space the layout constants
    # are written in. Unscaled, the sample lands mid-row on a scaled machine
    # and reads the same ink for both rows -- see 2e0838f, the same mistake in
    # a different costume.
    image = pixmap.toImage()
    ratio = image.devicePixelRatio() or 1.0
    px0, px1 = round(x0 * ratio), round(x1 * ratio)
    py0, py1 = round(y0 * ratio), round(y1 * ratio)
    return min(
        image.pixelColor(x, y).red() + image.pixelColor(x, y).green() + image.pixelColor(x, y).blue()
        for x in range(px0, px1)
        for y in range(py0, py1)
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


class TestTheReuseLastRegionToggle:
    """The row's one on/off control. It is on the row rather than in
    Settings because a preference nobody finds is a preference nobody has:
    this one shipped in Settings first and went unnoticed.
    """

    def test_it_is_off_by_default(self):
        assert Chooser(parent=None).reuse_last_region is False

    def test_seeding_it_does_not_emit(self):
        # Adopting stored config and the user clicking are different
        # events, and only the second is worth writing back -- the same
        # rule `set_after` follows.
        chooser = Chooser(parent=None)
        fired = []
        chooser.reuseLastRegionChanged.connect(fired.append)

        chooser.set_reuse_last_region(True)

        assert chooser.reuse_last_region is True
        assert fired == []

    def test_clicking_it_flips_and_announces(self):
        chooser = Chooser(parent=None)
        fired = []
        chooser.reuseLastRegionChanged.connect(fired.append)

        QTest.mouseClick(chooser.panel.reuse_toggle, Qt.MouseButton.LeftButton)

        assert fired == [True]
        assert chooser.reuse_last_region is True

    def test_clicking_it_again_turns_it_back_off(self):
        chooser = Chooser(parent=None)
        chooser.set_reuse_last_region(True)
        fired = []
        chooser.reuseLastRegionChanged.connect(fired.append)

        QTest.mouseClick(chooser.panel.reuse_toggle, Qt.MouseButton.LeftButton)

        assert fired == [False]
        assert chooser.reuse_last_region is False

    def test_it_sits_next_to_the_mode_it_modifies(self):
        # Before the destination and delay triggers, which answer a
        # different question entirely.
        chooser = Chooser(parent=None)
        panel = chooser.panel
        order = [panel.layout().itemAt(i).widget() for i in range(panel.layout().count())]

        assert order.index(panel.reuse_toggle) == order.index(panel.mode_trigger) + 1
        assert order.index(panel.reuse_toggle) < order.index(panel.after_trigger)

    def test_it_is_hidden_on_the_record_side(self):
        # Recording never pre-selects -- committing there arms a recording
        # -- so offering the control would promise something that side
        # does not do.
        chooser = Chooser(parent=None)
        chooser.panel.show()

        chooser.set_kind("record")

        assert chooser.panel.reuse_toggle.isVisibleTo(chooser.panel) is False

    def test_it_comes_back_on_the_stills_side(self):
        chooser = Chooser(parent=None)
        chooser.panel.show()
        chooser.set_kind("record")

        chooser.set_kind("stills")

        assert chooser.panel.reuse_toggle.isVisibleTo(chooser.panel) is True

    def test_hovering_it_explains_what_it_does(self):
        # Qt tooltips are a coin toss on an always-on-top frameless window,
        # so the row's own hint pill is what carries the explanation.
        chooser = Chooser(parent=None)

        chooser._on_reuse_hovered(True)

        assert chooser.hint._text == tokens.REUSE_HINT[False]

    def test_the_hint_says_how_to_turn_it_off_once_it_is_on(self):
        chooser = Chooser(parent=None)
        chooser.set_reuse_last_region(True)

        chooser._on_reuse_hovered(True)

        assert chooser.hint._text == tokens.REUSE_HINT[True]

    def test_leaving_it_restores_the_modes_own_hint(self):
        chooser = Chooser(parent=None)
        chooser._on_reuse_hovered(True)

        chooser._on_reuse_hovered(False)

        assert chooser.hint._text == tokens.MODE_NEXT_STEP["Region"]

    def test_both_hints_fit_the_pill_without_eliding(self):
        # The pill sizes itself to its text, and the row is centred on one
        # monitor -- a hint wider than the narrowest sane screen would hang
        # off it.
        from PyQt6.QtGui import QFontMetricsF

        from snipux.chooser import _font

        metrics = QFontMetricsF(_font(11.5, 400))
        for state, text in tokens.REUSE_HINT.items():
            width = metrics.horizontalAdvance(text)
            assert width <= 420, f"reuse hint for {state} is {width:.0f}px: {text!r}"


class TestFlippingKindDoesNotLeakTheDestination:
    """The two vocabularies overlap on `instant` and `save`, so a stills
    destination displaced by the record side used to stay displaced on the
    way back -- and `instant` takes the shot the moment the drag ends, with
    no overlay and no toolbar.
    """

    def test_the_stills_destination_comes_back(self):
        chooser = Chooser(parent=None)
        chooser.set_after("edit")

        chooser.set_kind("record")
        chooser.set_kind("stills")

        assert chooser.after == "edit"

    def test_it_survives_several_round_trips(self):
        chooser = Chooser(parent=None)
        chooser.set_after("review")

        for _ in range(3):
            chooser.set_kind("record")
            chooser.set_kind("stills")

        assert chooser.after == "review"

    def test_the_record_side_still_gets_its_own_default(self):
        chooser = Chooser(parent=None)
        chooser.set_after("edit")

        chooser.set_kind("record")

        assert chooser.after == tokens.RECORD_AFTER_DEFAULT

    def test_a_destination_chosen_on_the_record_side_is_kept_there(self):
        chooser = Chooser(parent=None)
        chooser.set_after("edit")
        chooser.set_kind("record")
        chooser.set_after("save")

        chooser.set_kind("stills")
        chooser.set_kind("record")

        assert chooser.after == "save"

    def test_a_shared_id_is_not_treated_as_displaced(self):
        # `save` means the same thing on both sides, so it is never
        # displaced and there is nothing to restore -- flipping back must
        # leave it alone rather than resurrect something older.
        chooser = Chooser(parent=None)
        chooser.set_after("save")

        chooser.set_kind("record")
        chooser.set_kind("stills")

        assert chooser.after == "save"


class TestTheTabModeNeedsABrowser:
    """`Tab` captures the frontmost browser's page area. With no browser to
    find -- none open, or a platform that cannot see other applications'
    windows at all -- the row says so rather than offering a capture that
    would produce nothing.
    """

    def test_it_is_greyed_with_a_reason_by_default(self):
        chooser = Chooser(parent=None)

        rows, _selected, _width = chooser._rows_for("mode")
        by_value = {value: (note, disabled) for value, _i, _l, note, _s, disabled in rows}

        note, disabled = by_value[tokens.TAB_MODE]
        assert disabled is True
        assert note == tokens.TAB_UNAVAILABLE

    def test_the_shortcut_is_inert_while_the_row_is_disabled(self):
        # A greyed row already swallows its own click; the shortcut key has
        # to leave the current mode alone the same way, or the keyboard
        # reaches a mode the menu says is unreachable.
        chooser = Chooser(parent=None)
        before = chooser.mode

        chooser.set_mode(tokens.TAB_MODE)

        assert chooser.mode == before

    def test_seeding_a_browser_makes_it_pickable(self):
        chooser = Chooser(parent=None)
        chooser.set_browser_available(True)

        chooser.set_mode(tokens.TAB_MODE)

        assert chooser.mode == tokens.TAB_MODE

    def test_it_is_greyed_on_the_record_side_even_with_a_browser(self):
        # Recording a browser page is sensible and the rect is the same
        # one; it is off because nothing has driven it end to end there.
        chooser = Chooser(parent=None)
        chooser.set_browser_available(True)
        chooser.set_kind("record")

        rows, _selected, _width = chooser._rows_for("mode")
        by_value = {value: (note, disabled) for value, _i, _l, note, _s, disabled in rows}

        note, disabled = by_value[tokens.TAB_MODE]
        assert disabled is True
        assert note == tokens.RECORD_DISABLED_MODES[tokens.TAB_MODE]
