"""The chooser row, headless (#66): its state machine, and what each of its
controls renders.

UI and state only -- nothing here calls into `recording.py` or a platform.
What needs a hosting `OverlayWindow` -- presses kept off the frame, placement
against real monitors, the collapse once a selection exists -- is in
test_overlay.py. This file builds `Chooser(parent=None)` directly.

Every size here is logical. `grab()` returns physical pixels, so a correct
42px row grabs 63px tall at a scale factor of 1.5: compare
`grab().deviceIndependentSize()`, never `grab().height()`.
"""

import math

import pytest
import time

from PyQt6.QtCore import QEvent, QPoint, QPointF, QRectF, QSizeF, Qt
from PyQt6.QtGui import QColor, QEnterEvent, QFontMetricsF, QImage, QPainter, QRegion
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QGraphicsOpacityEffect, QWidget

from snipux import chooser as chooser_module
from snipux import flowbars as flowbars_module
from snipux.chooser import (
    _AFTER_ROWS,
    _MenuRow,
    _RECORD_AFTER_ROWS,
    _RowSpec,
    Chooser,
    _font,
)
from snipux.design import tokens

METRIC = tokens.BarMetric
FONT = tokens.BarFont


@pytest.fixture(scope="module", autouse=True)
def qapp():
    # PyQt6 needs a live QApplication to construct any QWidget, even
    # offscreen -- matching the convention in test_overlay.py/test_design.py.
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


@pytest.fixture(autouse=True)
def _no_windows_left_behind():
    # A chooser built without a parent puts its row, pill and tab up as
    # windows of their own, and an open menu is a popup, which takes every
    # mouse event in the process. Left open, either one swallows the
    # synthetic input of whichever test runs next.
    yield
    for widget in QApplication.topLevelWidgets():
        if widget.isVisible():
            widget.close()


def _centre(widget):
    return QPoint(widget.width() // 2, widget.height() // 2)


def _click(widget):
    QTest.mouseClick(widget, Qt.MouseButton.LeftButton, pos=_centre(widget))


def _hover(widget, hovered=True):
    # Sent straight to the widget: a synthesised hover goes to whatever the
    # platform thinks is under the pointer, which offscreen is nothing here.
    if hovered:
        point = QPointF(1, 1)
        QApplication.sendEvent(widget, QEnterEvent(point, point, point))
    else:
        QApplication.sendEvent(widget, QEvent(QEvent.Type.Leave))


def _near(image, hex_colour, tolerance=28):
    """Whether any pixel of `image` is within `tolerance` of `hex_colour` on
    every channel. A glyph's antialiased stroke seldom lands exactly on it."""
    target = QColor(hex_colour)
    for y in range(image.height()):
        for x in range(image.width()):
            pixel = image.pixelColor(x, y)
            if (
                abs(pixel.red() - target.red()) <= tolerance
                and abs(pixel.green() - target.green()) <= tolerance
                and abs(pixel.blue() - target.blue()) <= tolerance
            ):
                return True
    return False


def _every_mode_available(chooser):
    chooser.set_browser_available(True)
    chooser.set_active_window_available(True)
    chooser.set_last_region(QSizeF(640, 480))


class TestTheKindDefaultsToStills:
    def test_a_fresh_chooser_starts_on_stills(self):
        chooser = Chooser(parent=None)

        assert chooser.kind == "stills"
        assert chooser.row.stills.is_active()
        assert not chooser.row.record.is_active()


class TestPickingTheKind:
    """Camera and record are a pair of buttons in one well. A click on either
    picks that side, rather than flipping whichever side is current."""

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

    def test_clicking_record_picks_record(self):
        chooser = Chooser(parent=None)

        _click(chooser.row.record)

        assert chooser.kind == "record"
        assert chooser.row.record.is_active()
        assert not chooser.row.stills.is_active()

    def test_clicking_stills_picks_stills(self):
        chooser = Chooser(parent=None)
        chooser.set_kind("record")

        _click(chooser.row.stills)

        assert chooser.kind == "stills"

    def test_clicking_the_side_already_picked_changes_nothing(self):
        chooser = Chooser(parent=None)
        emitted = []
        chooser.kindChanged.connect(emitted.append)

        _click(chooser.row.stills)

        assert chooser.kind == "stills"
        assert emitted == []

    def test_it_changes_only_kind_when_the_current_selection_still_fits(self):
        # Region is valid on both sides, so picking record with it already
        # chosen must not touch phase or mode, and must not announce a mode.
        chooser = Chooser(parent=None)
        mode_chosen = []
        fired = []
        chooser.modeChosen.connect(mode_chosen.append)
        chooser.fireImmediately.connect(fired.append)

        _click(chooser.row.record)

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
        chooser.collapse()

        chooser.reopen()

        assert chooser.kind == "record"
        assert chooser.phase == "choosing"


class TestKindChangedSignal:
    """`kindChanged` is how the row tells the window outside it which side
    is showing -- the stills bar and the recording pill are not the same
    furniture. The side itself is never persisted (bars/divergences.md 25),
    so every real flip still has to be announced.
    """

    def test_flipping_the_kind_emits_the_new_kind(self):
        chooser = Chooser(parent=None)
        emitted = []
        chooser.kindChanged.connect(emitted.append)

        chooser.set_kind("record")

        assert emitted == ["record"]

    def test_setting_the_same_kind_again_emits_nothing(self):
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


class TestDelayChangedSignal:
    """#73: the row's delay has to reach the capture, and `delayChanged` is
    the only way out of this widget -- nothing downstream reads `delay`.
    """

    def test_picking_a_delay_emits_it(self):
        chooser = Chooser(parent=None)
        emitted = []
        chooser.delayChanged.connect(emitted.append)

        chooser.set_delay("5s")

        assert emitted == ["5s"]

    def test_the_same_delay_again_emits_nothing(self):
        # The overlay seeds this back from the bar's popover; a signal here
        # would send the value straight back to where it came from.
        chooser = Chooser(parent=None)
        chooser.set_delay("5s")
        emitted = []
        chooser.delayChanged.connect(emitted.append)

        chooser.set_delay("5s")

        assert emitted == []

    def test_a_delay_that_is_not_offered_is_refused(self):
        chooser = Chooser(parent=None)
        emitted = []
        chooser.delayChanged.connect(emitted.append)

        chooser.set_delay("7s")

        assert chooser.delay == tokens.DELAY_DEFAULT
        assert emitted == []

    def test_clicking_the_flag_cycles_every_delay_and_back_to_none(self):
        chooser = Chooser(parent=None)
        emitted = []
        chooser.delayChanged.connect(emitted.append)

        for _ in tokens.DELAYS:
            _click(chooser.row.delay_flag)

        assert emitted == tokens.DELAYS[1:] + [tokens.DELAYS[0]]


class TestSwitchingToRecordSnapsAnUnavailableMode:
    # Browser stays off the record side: nothing has driven it end to end.
    @pytest.mark.parametrize("mode", ["Browser"])
    def test_it_snaps_to_region(self, mode):
        chooser = Chooser(parent=None)
        chooser.set_browser_available(True)
        chooser.set_mode(mode, announce=False)
        assert chooser.mode == mode

        chooser.set_kind("record")

        assert chooser.mode == "Region"

    def test_a_mode_already_valid_on_the_record_side_is_left_alone(self):
        chooser = Chooser(parent=None)
        chooser.set_mode("Full screen", announce=False)

        chooser.set_kind("record")

        assert chooser.mode == "Full screen"

    def test_switching_back_to_stills_needs_no_snap(self):
        chooser = Chooser(parent=None)
        chooser.set_browser_available(True)
        chooser.set_mode("Browser", announce=False)
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

        assert fired == [tokens.MONITOR_MODES[0]]
        assert chooser.phase == "choosing"

    def test_record_announces_it_as_chosen_instead(self):
        # Nothing is filmed from a pick: the record side arms the ready
        # stage, where Record is pressed.
        chooser = Chooser(parent=None)
        chooser.set_kind("record")
        fired, chosen = [], []
        chooser.fireImmediately.connect(fired.append)
        chooser.modeChosen.connect(chosen.append)

        chooser.set_mode("Full screen")

        assert fired == []
        assert chosen == ["Full screen"]
        assert chooser.phase == "choosing"


class TestFullScreenArmsWithMoreThanOneMonitor:
    """#53: which monitor is still a choice when there is more than one, so
    Full screen does not capture on the pick there
    (docs/design/bars/divergences.md 3).
    """

    def test_it_is_announced_as_chosen_on_a_desk_with_more_than_one(self):
        chooser = Chooser(parent=None)
        chooser.set_monitor_count(3)
        fired, chosen = [], []
        chooser.fireImmediately.connect(fired.append)
        chooser.modeChosen.connect(chosen.append)

        chooser.set_mode("Full screen")

        assert fired == []
        assert chosen == ["Full screen"]
        assert chooser.phase == "choosing"

    def test_one_monitor_still_fires_on_the_pick(self):
        chooser = Chooser(parent=None)
        chooser.set_monitor_count(1)
        fired = []
        chooser.fireImmediately.connect(fired.append)

        chooser.set_mode("Full screen")

        assert fired == ["Full screen"]

    @pytest.mark.parametrize("mode", [tokens.BROWSER_MODE, tokens.ACTIVE_WINDOW_MODE])
    def test_modes_aimed_at_something_else_fire_however_many_monitors(self, mode):
        # A page or a window is one rectangle wherever it is; there is no
        # monitor to choose.
        chooser = Chooser(parent=None)
        _every_mode_available(chooser)
        chooser.set_monitor_count(3)
        fired = []
        chooser.fireImmediately.connect(fired.append)

        chooser.set_mode(mode)

        assert fired == [mode]

    def test_the_hint_says_to_click_a_monitor(self):
        # "Grabs this monitor the moment you choose it" would be untrue.
        chooser = Chooser(parent=None)
        chooser.set_monitor_count(2)

        chooser.set_mode("Full screen", announce=False)

        assert chooser.hint.text == tokens.MULTI_MONITOR_NEXT_STEP["Full screen"]

    def test_with_one_monitor_the_hint_is_unchanged(self):
        chooser = Chooser(parent=None)

        chooser.set_mode("Full screen", announce=False)

        assert chooser.hint.text == tokens.MODE_NEXT_STEP["Full screen"]


class TestPickingAModeDoesNotArmIt:
    """#66: the row stays up until the user drags or clicks a window. A pick
    only says what the next drag or click takes; the overlay folds the row
    to its tab once a selection exists, and nothing in here can.
    """

    @pytest.mark.parametrize("mode", ["Region", "Window"])
    def test_the_row_stays_open(self, mode):
        chooser = Chooser(parent=None)
        chosen = []
        chooser.modeChosen.connect(chosen.append)

        chooser.set_mode(mode)

        assert chosen == [mode]
        assert chooser.phase == "choosing"

    def test_collapse_and_reopen_are_the_only_ways_between_row_and_tab(self):
        chooser = Chooser(parent=None)

        chooser.collapse()
        chooser.set_mode("Window")
        assert chooser.phase == "collapsed"

        chooser.reopen()
        assert chooser.phase == "choosing"

    def test_collapsing_closes_an_open_menu(self):
        chooser = Chooser(parent=None)
        _click(chooser.row.mode_chip)
        assert chooser._menu is not None

        chooser.collapse()

        assert chooser._menu is None
        assert not chooser.row.mode_chip.is_open()


class TestRecordSideModeSelectionIsInert:
    @pytest.mark.parametrize("mode", ["Browser"])
    def test_picking_a_disabled_mode_leaves_it_unchanged(self, mode):
        chooser = Chooser(parent=None)
        chooser.set_kind("record")

        chooser.set_mode(mode)

        assert chooser.mode == "Region"
        assert chooser.phase == "choosing"

    @pytest.mark.parametrize("key,mode", [("B", "Browser")])
    def test_the_shortcut_key_is_inert_too(self, key, mode):
        chooser = Chooser(parent=None)
        chooser.set_kind("record")

        chooser.handle_key(ord(key), key)

        assert chooser.mode == "Region"

    def test_window_is_live_on_the_record_side(self):
        # It resolves to a rect exactly like a dragged region does, which is
        # all the recorder ever wanted.
        chooser = Chooser(parent=None)
        chooser.set_kind("record")

        chooser.set_mode("Window")

        assert chooser.mode == "Window"


class TestEachControlShowsItsState:
    """Mode is the row's one label. Everything else is an icon that shows its
    state, with the explanation in its tooltip and the hint pill."""

    def test_the_mode_chip_names_and_draws_the_mode(self):
        chooser = Chooser(parent=None)
        _every_mode_available(chooser)

        for label, glyph, _note in tokens.CAPTURE_MODES:
            chooser.set_mode(label, announce=False)
            chip = chooser.row.mode_chip
            assert (chip.label, chip.glyph) == (label, glyph)

    def test_the_destination_is_an_icon_its_tooltip_names_on_both_sides(self):
        for kind, rows in (("stills", _AFTER_ROWS), ("record", _RECORD_AFTER_ROWS)):
            chooser = Chooser(parent=None)
            chooser.set_kind(kind)
            for value, glyph, label, note in rows:
                chooser.set_after(value)
                destination = chooser.row.destination
                assert destination.glyph == glyph, f"{value} on the {kind} side"
                assert label in destination.toolTip()
                assert note in destination.toolTip()
                assert destination.width() == METRIC.BTN

    @pytest.mark.parametrize(
        "kind, expected",
        [
            ("stills", ["save", "review", "instant", "edit"]),
            ("record", ["save", "open", "gif", "instant"]),
        ],
    )
    def test_a_click_cycles_todays_destinations(self, kind, expected):
        # docs/design/bars/divergences.md 4: the destinations do not change.
        chooser = Chooser(parent=None)
        chooser.set_kind(kind)
        seen = []

        for _ in expected:
            _click(chooser.row.destination)
            seen.append(chooser.after)

        assert seen == expected

    def test_a_click_is_remembered_like_a_pick(self):
        chooser = Chooser(parent=None)
        emitted = []
        chooser.afterChanged.connect(emitted.append)

        _click(chooser.row.destination)

        assert emitted == ["save"]

    def test_delay_shows_its_value_only_once_one_is_set(self):
        chooser = Chooser(parent=None)
        flag = chooser.row.delay_flag
        assert (flag.value, flag.is_armed()) == ("", False)

        chooser.set_delay("5s")
        assert (flag.value, flag.is_armed()) == ("5s", True)

        chooser.set_delay(tokens.DELAY_DEFAULT)
        assert (flag.value, flag.is_armed()) == ("", False)

    def test_a_set_delay_widens_its_flag_by_its_measured_value(self):
        chooser = Chooser(parent=None)
        flag = chooser.row.delay_flag
        bare = flag.width()

        chooser.set_delay("10s")

        measured = QFontMetricsF(_font(FONT.DELAY, mono=True)).horizontalAdvance("10s")
        assert 0 <= flag.width() - bare - METRIC.FLAG_GAP - measured < 1

    def test_the_delay_tooltip_says_what_is_set(self):
        chooser = Chooser(parent=None)
        assert chooser.row.delay_flag.toolTip() == tokens.DELAY_TOOLTIP_OFF

        chooser.set_delay("3s")

        assert chooser.row.delay_flag.toolTip() == tokens.DELAY_TOOLTIP_ON.format(delay="3s")


def _expected_row_width(label, delay="", hide=True):
    """`hide` now means "one flag beside Delay" -- Hide sensitive on the
    stills side, Record the pointer on the record side. The two are the same
    width and only one is ever shown, so the row measures the same either
    way.

    The width comes from the tokens and the measured text, worked out apart
    from the widget's own arithmetic. The label is measured in the face that
    resolves here, since IBM Plex is not bundled (#63).
    """
    chip = (
        2 * METRIC.BORDER + METRIC.CHIP_PAD_L + METRIC.CHIP_ICON + METRIC.CHIP_GAP
        + QFontMetricsF(_font(FONT.CHIP)).horizontalAdvance(label)
        + METRIC.CHIP_GAP + METRIC.CHEVRON + METRIC.CHIP_PAD_R
    )
    delay_flag = 2 * METRIC.FLAG_PAD_H + METRIC.ICON
    if delay:
        delay_flag += METRIC.FLAG_GAP + QFontMetricsF(
            _font(FONT.DELAY, mono=True)
        ).horizontalAdvance(delay)
    flags = 2 * METRIC.WELL_PAD + delay_flag
    if hide:
        flags += METRIC.BTN + METRIC.WELL_GAP
    kinds = 2 * METRIC.WELL_PAD + 2 * METRIC.BTN + METRIC.WELL_GAP
    divider = 2 * METRIC.DIVIDER_MARGIN + 1
    return (
        2 * (METRIC.BORDER + METRIC.PAD)
        + kinds + chip + divider + METRIC.BTN + flags
        + 4 * METRIC.GAP
    )


class TestTheRowsSize:
    """ROW_H tall, and as wide as its tokens plus its measured text. The
    handoff's 382 is measured in IBM Plex Sans, which is not bundled, so it
    is never asserted here (docs/design/bars/divergences.md)."""

    def test_it_is_row_h_tall(self):
        chooser = Chooser(parent=None)

        assert chooser.row.grab().deviceIndependentSize().height() == METRIC.ROW_H

    @pytest.mark.parametrize(
        "mode", [m[0] for m in tokens.CAPTURE_MODES] + [tokens.LAST_REGION_MODE]
    )
    def test_its_width_is_its_tokens_plus_the_measured_mode_label(self, mode):
        chooser = Chooser(parent=None)
        _every_mode_available(chooser)

        chooser.set_mode(mode, announce=False)

        width = chooser.row.grab().deviceIndependentSize().width()
        # Text widths are rounded up, so a label is never clipped.
        assert 0 <= width - _expected_row_width(mode) < 2

    def test_a_set_delay_widens_it_by_its_measured_value(self):
        chooser = Chooser(parent=None)

        chooser.set_delay("10s")

        width = chooser.row.grab().deviceIndependentSize().width()
        assert 0 <= width - _expected_row_width("Region", delay="10s") < 2

    def test_the_record_side_swaps_one_flag_for_the_other(self):
        """Hide sensitive goes (a recording has no frozen frame to read) and
        Record the pointer takes its place, so the row is the same width on
        both sides rather than shrinking as it used to."""
        chooser = Chooser(parent=None)

        chooser.set_kind("record")

        width = chooser.row.grab().deviceIndependentSize().width()
        assert 0 <= width - _expected_row_width("Region") < 2
        # isVisibleTo, not isVisible: an unshown row's children all report
        # False, which would make both halves of this pass for the wrong
        # reason.
        assert not chooser.row.hide_flag.isVisibleTo(chooser.row)
        assert chooser.row.cursor_flag.isVisibleTo(chooser.row)


class TestEachControlRendersItsState:
    def test_the_picked_kind_is_lit(self):
        chooser = Chooser(parent=None)
        idle = chooser.row.record.grab().toImage()

        chooser.set_kind("record")

        assert chooser.row.record.grab().toImage() != idle

    def test_record_is_a_camcorder_glyph_in_its_lit_colour(self):
        # A camcorder, not the handoff's filled circle (bars/divergences.md
        # 25): the glyph is stroked in the lit colour over the lit fill, so
        # the centre of the button is inside its hollow body.
        chooser = Chooser(parent=None)
        chooser.set_kind("record")

        image = chooser.row.record.grab().toImage()

        ratio = image.devicePixelRatio()
        centre = round(METRIC.BTN / 2 * ratio)
        assert _near(image, tokens.BarColor.REC_ON_FG)
        # Hollow where the handoff's dot was solid: the middle of the body
        # is nowhere near the glyph's own colour.
        assert not _near(image.copy(centre - 1, centre - 1, 3, 3), tokens.BarColor.REC_ON_FG)

    def test_an_armed_flag_is_the_soft_accent_on_an_accent_wash(self):
        chooser = Chooser(parent=None)
        flag = chooser.row.hide_flag
        idle = flag.grab().toImage()
        assert _near(idle, tokens.BarColor.FLAG_OFF_FG)
        assert not _near(idle, tokens.BarColor.ACCENT_SOFT)

        chooser.set_hide_sensitive(True)

        armed = flag.grab().toImage()
        assert _near(armed, tokens.BarColor.ACCENT_SOFT)
        # The wash, away from the glyph at the flag's left edge.
        ratio = armed.devicePixelRatio()
        at = (round(3 * ratio), round(METRIC.BTN / 2 * ratio))
        assert armed.pixelColor(*at) != idle.pixelColor(*at)

    def test_an_unavailable_flag_is_greyed(self):
        chooser = Chooser(parent=None)
        usable = chooser.row.hide_flag.grab().toImage()

        chooser.set_hide_sensitive_available(False, "nope")

        greyed = chooser.row.hide_flag.grab().toImage()
        assert greyed != usable
        assert _near(greyed, tokens.BarColor.TOOL_DISABLED_FG)

    def test_the_mode_glyph_is_the_soft_accent(self):
        chooser = Chooser(parent=None)

        assert _near(chooser.row.mode_chip.grab().toImage(), tokens.BarColor.ACCENT_SOFT)

    def test_the_open_chip_shows_it(self):
        chooser = Chooser(parent=None)
        closed = chooser.row.mode_chip.grab().toImage()

        chooser.row.mode_chip.set_open(True)

        assert chooser.row.mode_chip.grab().toImage() != closed

    def test_each_destination_draws_its_own_glyph(self):
        chooser = Chooser(parent=None)
        chooser.set_after("instant")
        instant = chooser.row.destination.grab().toImage()

        chooser.set_after("review")

        assert chooser.row.destination.grab().toImage() != instant


class TestTheModeMenu:
    @staticmethod
    def _open(chooser):
        _click(chooser.row.mode_chip)
        return chooser._menu

    @staticmethod
    def _tooltips(kind):
        chooser = Chooser(parent=None)
        _every_mode_available(chooser)
        chooser.set_kind(kind)
        rows, _last_region = chooser._mode_rows()
        return {spec.value: spec.tooltip for spec in rows}

    def test_one_row_per_mode_then_a_rule_then_last_region(self):
        chooser = Chooser(parent=None)

        menu = self._open(chooser)

        layout = menu.layout()
        widgets = [layout.itemAt(i).widget() for i in range(layout.count())]
        values = [widget.value for widget in widgets if isinstance(widget, _MenuRow)]
        assert values == [m[0] for m in tokens.CAPTURE_MODES] + [tokens.LAST_REGION_MODE]
        assert widgets.index(menu.rule) == len(tokens.CAPTURE_MODES)
        assert menu.width() == METRIC.MENU_W_MODE

    def test_it_is_a_top_level_popup_so_it_paints_over_the_hint_pill(self):
        # Held, or the chooser -- and the menu, a child of its row -- is
        # collected before the assertions run.
        chooser = Chooser(parent=None)
        menu = self._open(chooser)

        assert menu.isWindow()
        assert menu.windowType() == Qt.WindowType.Popup

    def test_each_row_is_glyph_label_and_shortcut_with_no_note(self):
        chooser = Chooser(parent=None)
        _every_mode_available(chooser)
        keys = {mode: key for key, mode in tokens.MODE_KEYS.items()}

        rows, _last_region = chooser._mode_rows()

        for spec, (label, glyph, _note) in zip(rows, tokens.CAPTURE_MODES):
            assert (spec.label, spec.glyph, spec.shortcut) == (label, glyph, keys[label])
            assert spec.subtitle == ""

    def test_the_notes_are_the_tooltips_now(self):
        tooltips = self._tooltips("stills")

        assert list(tooltips.values()) == [note for _l, _g, note in tokens.CAPTURE_MODES]

    def test_the_record_side_tooltip_says_a_window_is_filmed_where_it_is(self):
        # The recorder is handed a rectangle once and does not follow the
        # window afterwards, which is the surprise worth naming up front.
        tooltip = self._tooltips("record")["Window"]

        assert tooltip == tokens.RECORD_MODE_NOTE["Window"]
        assert "where it is" in tooltip.lower()

    def test_window_and_full_screen_do_not_describe_the_same_thing(self):
        # "if you're capturing a window, you're capturing a full screen?"
        tooltips = self._tooltips("stills")

        assert "window" in tooltips["Window"].lower()
        assert "monitor" in tooltips["Full screen"].lower()

    def test_window_says_what_it_asks_of_you_on_both_sides(self):
        for kind in ("stills", "record"):
            assert "click" in self._tooltips(kind)["Window"].lower()

    def test_window_and_active_window_cannot_be_confused_at_a_glance(self):
        for kind in ("stills", "record"):
            tooltips = self._tooltips(kind)
            assert tooltips["Window"] != tooltips[tokens.ACTIVE_WINDOW_MODE]
            assert "click" not in tooltips[tokens.ACTIVE_WINDOW_MODE].lower()

    def test_picking_a_row_adopts_the_mode_and_closes_the_menu(self):
        chooser = Chooser(parent=None)
        chosen = []
        chooser.modeChosen.connect(chosen.append)
        menu = self._open(chooser)

        _click(menu._rows["Window"])

        assert chooser.mode == "Window"
        assert chosen == ["Window"]
        assert chooser._menu is None
        assert not chooser.row.mode_chip.is_open()

    def test_the_chosen_row_is_ticked(self):
        chooser = Chooser(parent=None)
        menu = self._open(chooser)

        assert menu._rows["Region"].selected
        assert not menu._rows["Window"].selected

    def test_clicking_the_chip_again_closes_it(self):
        chooser = Chooser(parent=None)
        self._open(chooser)

        _click(chooser.row.mode_chip)

        assert chooser._menu is None

    def test_a_menu_that_closes_itself_leaves_the_chip_closed(self):
        # A popup closes on a click anywhere else, and on Escape.
        chooser = Chooser(parent=None)
        menu = self._open(chooser)

        menu.close()

        assert chooser._menu is None
        assert not chooser.row.mode_chip.is_open()


def _brightest(image, x0, x1):
    return max(
        image.pixelColor(x, y).lightness()
        for x in range(max(0, x0), min(image.width(), x1))
        for y in range(image.height())
    )


class TestAGreyedModeRowCarriesItsReason:
    """A mode that cannot work stays in the menu, greyed, with its reason in
    the subtitle slot Last region uses for its dimensions."""

    @staticmethod
    def _spec(chooser, value):
        rows, last_region = chooser._mode_rows()
        return {spec.value: spec for spec in rows + [last_region]}[value]

    def test_browser_is_greyed_with_its_reason_by_default(self):
        spec = self._spec(Chooser(parent=None), tokens.BROWSER_MODE)

        assert (spec.disabled, spec.subtitle) == (True, tokens.BROWSER_UNAVAILABLE)

    def test_browser_is_greyed_on_the_record_side_even_with_a_browser(self):
        chooser = Chooser(parent=None)
        chooser.set_browser_available(True)
        chooser.set_kind("record")

        spec = self._spec(chooser, tokens.BROWSER_MODE)

        assert (spec.disabled, spec.subtitle) == (
            True, tokens.RECORD_DISABLED_MODES[tokens.BROWSER_MODE]
        )

    def test_active_window_is_greyed_with_its_reason_by_default(self):
        spec = self._spec(Chooser(parent=None), tokens.ACTIVE_WINDOW_MODE)

        assert (spec.disabled, spec.subtitle) == (True, tokens.ACTIVE_WINDOW_UNAVAILABLE)

    def test_active_window_says_when_the_platform_cannot_name_a_window(self):
        chooser = Chooser(parent=None)

        chooser.set_active_window_available(False, tokens.ACTIVE_WINDOW_UNSUPPORTED)

        assert self._spec(chooser, tokens.ACTIVE_WINDOW_MODE).subtitle == (
            tokens.ACTIVE_WINDOW_UNSUPPORTED
        )

    def test_stills_offers_every_mode_once_each_is_possible(self):
        chooser = Chooser(parent=None)
        _every_mode_available(chooser)

        rows, last_region = chooser._mode_rows()

        assert not any(spec.disabled for spec in rows + [last_region])

    def test_record_leaves_region_full_screen_and_the_windows_enabled(self):
        chooser = Chooser(parent=None)
        _every_mode_available(chooser)
        chooser.set_kind("record")

        for mode in ("Region", "Full screen", "Window", tokens.ACTIVE_WINDOW_MODE):
            assert self._spec(chooser, mode).disabled is False, mode

    def test_a_greyed_row_paints_its_reason_under_its_label(self):
        spec = _RowSpec(
            "Browser", "panel", "Browser", "B",
            subtitle=tokens.BROWSER_UNAVAILABLE, disabled=True,
        )
        row, other = _MenuRow(spec), _MenuRow(spec._replace(subtitle="x"))
        for each in (row, other):
            each.resize(METRIC.MENU_W_MODE - 2 * METRIC.MENU_PAD, each.height())
        pad_v, _pad_h = METRIC.MENU_ROW_PAD

        size = row.grab().deviceIndependentSize()

        # Within a physical pixel: 39 logical px is 58.5 physical at 1.5,
        # which the grab rounds to 59 and reports back as 39.33.
        expected = 2 * pad_v + math.ceil(
            FONT.MENU_LABEL[0] + METRIC.MENU_NOTE_GAP + FONT.MENU_NOTE[0]
        )
        assert abs(size.height() - expected) < 1
        assert row.grab().toImage() != other.grab().toImage()

    def test_a_disabled_row_swallows_its_press_but_never_clicks(self):
        # SNX-108's fix still applies to a disabled row -- the press must not
        # reach the frame behind the menu -- but the release must not click.
        row = _MenuRow(_RowSpec("Browser", "panel", "Browser", "B", subtitle="nope", disabled=True))
        row.resize(METRIC.MENU_W_MODE, row.height())
        clicked = []
        row.clicked.connect(clicked.append)

        _click(row)

        assert clicked == []

    def test_an_enabled_row_still_clicks_for_comparison(self):
        row = _MenuRow(_RowSpec("Region", "crop", "Region", "R"))
        row.resize(METRIC.MENU_W_MODE, row.height())
        clicked = []
        row.clicked.connect(clicked.append)

        _click(row)

        assert clicked == ["Region"]

    def test_a_disabled_rows_shortcut_is_dimmer_than_an_enabled_ones(self):
        # At full strength a disabled row's letter reads as if the key still
        # did something. Painted on the menu's own dark ground, where dimmer
        # means darker.
        width = METRIC.MENU_W_MODE - 2 * METRIC.MENU_PAD
        _pad_v, pad_h = METRIC.MENU_ROW_PAD
        right = width - pad_h - METRIC.MENU_TICK - METRIC.MENU_ROW_GAP
        left = right - math.ceil(
            QFontMetricsF(_font(FONT.MENU_SHORTCUT, mono=True)).horizontalAdvance("W")
        )

        def ink(disabled):
            row = _MenuRow(_RowSpec("Window", "window", "Window", "W", disabled=disabled))
            row.resize(width, row.height())
            image = QImage(row.size(), QImage.Format.Format_ARGB32)
            image.fill(QColor(tokens.BarColor.MENU_BG))
            painter = QPainter(image)
            row.render(painter, QPoint(), QRegion(), QWidget.RenderFlag.DrawChildren)
            painter.end()
            return _brightest(image, left, right)

        assert ink(disabled=True) < ink(disabled=False)

    def test_every_reason_fits_its_subtitle_slot(self):
        # The column a row gives its label and subtitle is what is left of
        # the menu's width after the glyph, the tick and that row's own
        # shortcut. Measured, and capped in characters as well: the
        # offscreen face this suite measures is narrower than some a user
        # has -- Segoe UI measured about half as wide again.
        _pad_v, pad_h = METRIC.MENU_ROW_PAD
        shortcut_font = QFontMetricsF(_font(FONT.MENU_SHORTCUT, mono=True))
        reason_font = QFontMetricsF(_font(FONT.MENU_NOTE))
        full = (
            METRIC.MENU_W_MODE - 2 * METRIC.MENU_PAD - 2 * pad_h - METRIC.MENU_ROW_ICON
            - METRIC.MENU_TICK - 3 * METRIC.MENU_ROW_GAP
        )
        keys = {mode: key for key, mode in tokens.MODE_KEYS.items()}
        cases = [
            (tokens.BROWSER_UNAVAILABLE, keys[tokens.BROWSER_MODE]),
            (tokens.ACTIVE_WINDOW_UNAVAILABLE, keys[tokens.ACTIVE_WINDOW_MODE]),
            (tokens.ACTIVE_WINDOW_UNSUPPORTED, keys[tokens.ACTIVE_WINDOW_MODE]),
            (tokens.LAST_REGION_NONE, tokens.LAST_REGION_SHORTCUT),
            (tokens.LAST_REGION_OFF_DESK, tokens.LAST_REGION_SHORTCUT),
        ] + [(reason, keys[mode]) for mode, reason in tokens.RECORD_DISABLED_MODES.items()]

        for reason, shortcut in cases:
            budget = full - math.ceil(shortcut_font.horizontalAdvance(shortcut))
            assert reason_font.horizontalAdvance(reason) <= budget, reason
            # 30 characters fit beside a one-letter shortcut on a real
            # screen; the cap shrinks with the room.
            assert len(reason) <= 30 * budget / (full - shortcut_font.horizontalAdvance("W")), reason


class TestLastRegionIsAMode:
    """#66: Last region answers "what to capture", so it is a mode -- a row
    under the rule, subtitled with the dimensions it restores -- rather than
    a toggle beside the mode."""

    def test_it_is_greyed_until_something_was_captured(self):
        chooser = Chooser(parent=None)

        _rows, spec = chooser._mode_rows()

        assert (spec.disabled, spec.subtitle) == (True, tokens.LAST_REGION_NONE)

    def test_its_subtitle_is_the_dimensions_it_restores_in_mono(self):
        chooser = Chooser(parent=None)

        chooser.set_last_region(QSizeF(1017, 562))

        _rows, spec = chooser._mode_rows()
        assert (spec.disabled, spec.subtitle, spec.subtitle_mono) == (
            False, "1017 × 562", True
        )
        assert (spec.label, spec.glyph, spec.shortcut) == (
            tokens.LAST_REGION_MODE, tokens.LAST_REGION_GLYPH, tokens.LAST_REGION_SHORTCUT
        )

    def test_a_region_off_these_monitors_says_so(self):
        chooser = Chooser(parent=None)

        chooser.set_last_region(None, tokens.LAST_REGION_OFF_DESK)

        _rows, spec = chooser._mode_rows()
        assert (spec.disabled, spec.subtitle) == (True, tokens.LAST_REGION_OFF_DESK)

    def test_greyed_it_cannot_be_chosen(self):
        chooser = Chooser(parent=None)

        chooser.set_mode(tokens.LAST_REGION_MODE)
        chooser.handle_key(Qt.Key.Key_R, "R", Qt.KeyboardModifier.ShiftModifier)

        assert chooser.mode == "Region"

    def test_choosing_it_restores_at_once_on_the_stills_side(self):
        chooser = Chooser(parent=None)
        chooser.set_last_region(QSizeF(640, 480))
        fired = []
        chooser.fireImmediately.connect(fired.append)

        chooser.set_mode(tokens.LAST_REGION_MODE)

        assert fired == [tokens.LAST_REGION_MODE]
        assert chooser.phase == "choosing"

    def test_on_the_record_side_it_is_announced_as_chosen(self):
        chooser = Chooser(parent=None)
        chooser.set_last_region(QSizeF(640, 480))
        chooser.set_kind("record")
        fired, chosen = [], []
        chooser.fireImmediately.connect(fired.append)
        chooser.modeChosen.connect(chosen.append)

        chooser.set_mode(tokens.LAST_REGION_MODE)

        assert (fired, chosen) == ([], [tokens.LAST_REGION_MODE])

    def test_shift_r_chooses_it(self):
        chooser = Chooser(parent=None)
        chooser.set_last_region(QSizeF(640, 480))

        handled = chooser.handle_key(Qt.Key.Key_R, "R", Qt.KeyboardModifier.ShiftModifier)

        assert handled
        assert chooser.mode == tokens.LAST_REGION_MODE

    def test_plain_r_is_still_region(self):
        chooser = Chooser(parent=None)
        chooser.set_last_region(QSizeF(640, 480))
        chooser.set_mode("Window", announce=False)

        chooser.handle_key(Qt.Key.Key_R, "r")

        assert chooser.mode == "Region"

    def test_picking_it_and_picking_away_are_what_the_next_snip_opens_on(self):
        chooser = Chooser(parent=None)
        chooser.set_last_region(QSizeF(640, 480))
        emitted = []
        chooser.reuseLastRegionChanged.connect(emitted.append)

        chooser.set_mode(tokens.LAST_REGION_MODE)
        chooser.set_mode(tokens.LAST_REGION_MODE)
        chooser.set_mode("Window")
        chooser.set_mode("Region")

        assert emitted == [True, False]

    def test_seeding_it_opens_on_it_without_a_signal(self):
        chooser = Chooser(parent=None)
        chooser.set_last_region(QSizeF(640, 480))
        emitted = []
        for signal in (chooser.reuseLastRegionChanged, chooser.modeChosen, chooser.fireImmediately):
            signal.connect(emitted.append)

        chooser.set_reuse_last_region(True)

        assert chooser.mode == tokens.LAST_REGION_MODE
        assert chooser.reuse_last_region is True
        assert emitted == []

    def test_seeding_it_with_nothing_to_restore_leaves_region(self):
        chooser = Chooser(parent=None)

        chooser.set_reuse_last_region(True)

        assert chooser.mode == "Region"

    def test_seeding_it_on_the_record_side_leaves_region(self):
        # Opening a recording on a rectangle would arm the recording.
        chooser = Chooser(parent=None)
        chooser.set_last_region(QSizeF(640, 480))
        chooser.set_kind("record")

        chooser.set_reuse_last_region(True)

        assert chooser.mode == "Region"

    def test_losing_what_it_restores_falls_back_to_region(self):
        chooser = Chooser(parent=None)
        chooser.set_last_region(QSizeF(640, 480))
        chooser.set_mode(tokens.LAST_REGION_MODE, announce=False)

        chooser.set_last_region(None, tokens.LAST_REGION_OFF_DESK)

        assert chooser.mode == "Region"

    def test_its_hint_is_its_own(self):
        chooser = Chooser(parent=None)
        chooser.set_last_region(QSizeF(640, 480))

        chooser.set_mode(tokens.LAST_REGION_MODE, announce=False)

        assert (chooser.hint.glyph, chooser.hint.text) == (
            tokens.LAST_REGION_GLYPH, tokens.MODE_NEXT_STEP[tokens.LAST_REGION_MODE]
        )


class TestTheHideSensitiveFlag:
    def test_it_is_off_by_default(self):
        assert Chooser(parent=None).hide_sensitive is False

    def test_seeding_it_does_not_emit(self):
        chooser = Chooser(parent=None)
        fired = []
        chooser.hideSensitiveChanged.connect(fired.append)

        chooser.set_hide_sensitive(True)

        assert chooser.hide_sensitive is True
        assert fired == []

    def test_clicking_it_flips_and_announces(self):
        chooser = Chooser(parent=None)
        fired = []
        chooser.hideSensitiveChanged.connect(fired.append)

        _click(chooser.row.hide_flag)
        _click(chooser.row.hide_flag)

        assert fired == [True, False]
        assert chooser.hide_sensitive is False

    def test_it_does_not_change_the_mode(self):
        chooser = Chooser(parent=None)

        _click(chooser.row.hide_flag)

        assert chooser.mode == "Region"

    def test_it_is_the_eye_with_a_strike_never_the_blur_droplet(self):
        glyph = Chooser(parent=None).row.hide_flag._glyph

        assert glyph == "eyeOff"
        assert glyph != "blur"

    def test_it_shares_a_well_with_delay_as_the_kinds_share_theirs(self):
        row = Chooser(parent=None).row

        assert row.hide_flag.parent() is row.flag_well
        assert row.delay_flag.parent() is row.flag_well
        assert row.stills.parent() is row.kind_well
        assert row.record.parent() is row.kind_well

    def test_it_is_hidden_on_the_record_side_and_comes_back(self):
        # Recognition reads a frozen frame, and a recording has none.
        chooser = Chooser(parent=None)

        chooser.set_kind("record")
        assert chooser.row.hide_flag.isHidden()

        chooser.set_kind("stills")
        assert not chooser.row.hide_flag.isHidden()

    def test_its_tooltip_and_hint_say_what_it_covers(self):
        chooser = Chooser(parent=None)
        _hover(chooser.row.hide_flag)
        assert chooser.hint.text == tokens.HIDE_SENSITIVE_HINT[False]
        assert chooser.row.hide_flag.toolTip() == tokens.HIDE_SENSITIVE_HINT[False]

        chooser.set_hide_sensitive(True)

        assert chooser.hint.text == tokens.HIDE_SENSITIVE_HINT[True]
        assert chooser.row.hide_flag.toolTip() == tokens.HIDE_SENSITIVE_HINT[True]

    def test_leaving_it_restores_the_modes_own_hint(self):
        chooser = Chooser(parent=None)
        _hover(chooser.row.hide_flag)

        _hover(chooser.row.hide_flag, hovered=False)

        assert chooser.hint.text == tokens.MODE_NEXT_STEP["Region"]

    def test_both_hints_fit_the_pill_without_eliding(self):
        # The pill sizes itself to its text, and the row is centred on one
        # monitor -- a hint wider than the narrowest sane screen would hang
        # off it.
        metrics = QFontMetricsF(_font(FONT.HINT))
        for state, text in tokens.HIDE_SENSITIVE_HINT.items():
            width = metrics.horizontalAdvance(text)
            assert width <= 420, f"hide-sensitive hint for {state} is {width:.0f}px: {text!r}"


class TestHideSensitiveWhenTextCannotBeRead:
    """Greyed with a reason, not hidden: a missing feature should not look
    like a broken one."""

    def test_available_until_told_otherwise(self):
        assert Chooser(parent=None).hide_sensitive_available is True

    def test_an_unavailable_flag_ignores_clicks(self):
        chooser = Chooser(parent=None)
        chooser.set_hide_sensitive_available(False, "Windows only for now")
        fired = []
        chooser.hideSensitiveChanged.connect(fired.append)

        _click(chooser.row.hide_flag)

        assert fired == []
        assert chooser.hide_sensitive is False

    def test_hovering_an_unavailable_flag_says_why(self):
        chooser = Chooser(parent=None)
        chooser.set_hide_sensitive_available(False, "Windows only for now")

        _hover(chooser.row.hide_flag)

        assert chooser.hint.text == "Windows only for now"
        assert chooser.row.hide_flag.toolTip() == "Windows only for now"

    def test_it_stays_on_the_row(self):
        chooser = Chooser(parent=None)

        chooser.set_hide_sensitive_available(False, "Windows only for now")

        assert not chooser.row.hide_flag.isHidden()

    def test_it_can_become_available_again(self):
        chooser = Chooser(parent=None)
        chooser.set_hide_sensitive_available(False, "nope")

        chooser.set_hide_sensitive_available(True)
        _click(chooser.row.hide_flag)

        assert chooser.hide_sensitive is True


class TestHoveringAControlBorrowsTheHint:
    """Qt's tooltips are a coin toss on an always-on-top frameless window,
    so the row's one line of prose explains whatever is under the pointer."""

    def test_the_destination_names_itself_there(self):
        chooser = Chooser(parent=None)

        _hover(chooser.row.destination)

        assert chooser.hint.text == chooser.row.destination.toolTip()

    def test_a_click_while_hovering_updates_what_it_says(self):
        chooser = Chooser(parent=None)
        _hover(chooser.row.delay_flag)
        assert chooser.hint.text == tokens.DELAY_TOOLTIP_OFF

        _click(chooser.row.delay_flag)

        assert chooser.hint.text == tokens.DELAY_TOOLTIP_ON.format(delay=tokens.DELAYS[1])

    @pytest.mark.parametrize("kind", ["stills", "record"])
    def test_each_kind_names_itself(self, kind):
        chooser = Chooser(parent=None)

        _hover(getattr(chooser.row, kind))

        assert chooser.hint.text == tokens.KIND_TOOLTIP[kind]

    def test_leaving_gives_the_hint_back_to_the_mode(self):
        chooser = Chooser(parent=None)
        _hover(chooser.row.destination)

        _hover(chooser.row.destination, hovered=False)

        assert chooser.hint.text == tokens.MODE_NEXT_STEP["Region"]


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
        chooser = Chooser(parent=None)
        chooser.set_after("save")

        chooser.set_kind("record")
        chooser.set_kind("stills")

        assert chooser.after == "save"


class TestTheDestinationVocabularies:
    def test_stills_offers_instant_edit_save_and_review(self):
        assert [value for value, *_rest in _AFTER_ROWS] == ["instant", "edit", "save", "review"]

    def test_record_offers_copy_save_open_and_gif(self):
        assert [value for value, *_rest in _RECORD_AFTER_ROWS] == [
            "instant", "save", "open", "gif",
        ]

    def test_the_two_lists_agree_on_what_the_shared_ids_mean(self):
        stills = {value for value, *_rest in _AFTER_ROWS}
        record = {value for value, *_rest in _RECORD_AFTER_ROWS}

        assert stills & record == {"instant", "save"}

    def test_stills_ids_still_round_trip_through_storage(self):
        # `load_after_capture` validates against `tokens.AFTER_CAPTURE`, so
        # a destination the row offers but storage rejects would fall back to
        # the default the moment it was read back.
        from snipux import setup_desktop

        for identifier, *_rest in _AFTER_ROWS:
            setup_desktop.save_after_capture(identifier)
            assert setup_desktop.load_after_capture() == identifier

    def test_the_notes_cover_exactly_the_stills_destinations(self):
        # Two surfaces, two lengths of prose, one list of destinations.
        assert set(tokens.CHOOSER_AFTER_NOTE) == {
            value for value, _label, _description in tokens.AFTER_CAPTURE
        }

    def test_the_notes_cover_exactly_the_record_destinations(self):
        assert set(tokens.CHOOSER_RECORD_AFTER_NOTE) == {
            value for value, _label, _description in tokens.RECORDING_AFTER
        }


class TestActiveWindowNeedsAWindowToTake:
    def test_the_shortcut_is_inert_while_the_row_is_disabled(self):
        chooser = Chooser(parent=None)

        chooser.handle_key(ord("A"), "A")

        assert chooser.mode == "Region"

    def test_with_a_window_found_choosing_it_fires(self):
        chooser = Chooser(parent=None)
        chooser.set_active_window_available(True)
        fired, chosen = [], []
        chooser.fireImmediately.connect(fired.append)
        chooser.modeChosen.connect(chosen.append)

        chooser.set_mode(tokens.ACTIVE_WINDOW_MODE)

        assert (fired, chosen) == ([tokens.ACTIVE_WINDOW_MODE], [])

    def test_browser_needs_a_browser_to_be_picked(self):
        chooser = Chooser(parent=None)
        chooser.set_mode(tokens.BROWSER_MODE)
        assert chooser.mode == "Region"

        chooser.set_browser_available(True)
        chooser.set_mode(tokens.BROWSER_MODE)

        assert chooser.mode == tokens.BROWSER_MODE


class TestTheCollapsedTab:
    """What the row folds to once a selection exists: 22px on the same edge,
    at 70% until the pointer is on it."""

    def test_it_is_tab_h_tall(self):
        chooser = Chooser(parent=None)

        assert chooser.tab.grab().deviceIndependentSize().height() == METRIC.TAB_H

    def test_it_carries_the_mode_and_then_the_destination(self):
        chooser = Chooser(parent=None)
        chooser.set_mode("Window", announce=False)
        chooser.set_after("review")

        assert (chooser.tab.mode, chooser.tab.tail) == ("Window", "then Review")

    def test_it_carries_the_eye_strike_while_hide_sensitive_is_on(self):
        chooser = Chooser(parent=None)
        bare_width = chooser.tab.width()
        bare = chooser.tab.grab().toImage()

        chooser.set_hide_sensitive(True)

        assert chooser.tab.carries_hide_sensitive
        assert chooser.tab.width() == bare_width + 2 * METRIC.TAB_GAP + 1 + METRIC.TAB_ICON
        assert chooser.tab.grab().toImage() != bare

    def test_not_while_hide_sensitive_cannot_work(self):
        chooser = Chooser(parent=None)
        chooser.set_hide_sensitive(True)

        chooser.set_hide_sensitive_available(False, "nope")

        assert not chooser.tab.carries_hide_sensitive

    def test_not_on_the_record_side(self):
        chooser = Chooser(parent=None)
        chooser.set_hide_sensitive(True)

        chooser.set_kind("record")

        assert not chooser.tab.carries_hide_sensitive

    def test_it_is_70_percent_until_the_pointer_is_on_it(self):
        chooser = Chooser(parent=None)
        assert chooser.tab.opacity() == pytest.approx(METRIC.TAB_OPACITY)

        _hover(chooser.tab)
        assert chooser.tab.opacity() == pytest.approx(1.0)

        _hover(chooser.tab, hovered=False)
        assert chooser.tab.opacity() == pytest.approx(METRIC.TAB_OPACITY)

    def test_clicking_it_reopens_the_row_as_it_was(self):
        chooser = Chooser(parent=None)
        chooser.set_mode("Window", announce=False)
        chooser.set_delay("5s")
        chooser.collapse()

        _click(chooser.tab)

        assert chooser.phase == "choosing"
        assert (chooser.mode, chooser.delay) == ("Window", "5s")

    def test_space_reopens_it_and_is_not_the_rows_otherwise(self):
        chooser = Chooser(parent=None)
        assert chooser.handle_key(Qt.Key.Key_Space, " ") is False

        chooser.collapse()

        assert chooser.handle_key(Qt.Key.Key_Space, " ") is True
        assert chooser.phase == "choosing"


class TestTheRowIsAFillNotAnOpacity:
    """Alpha is not opacity: the row is a translucent fill with fully opaque
    controls. `windowOpacity` would wash the icons out with the ground; the
    tab's 70% is the one real opacity.

    Built here with no overlay behind it, there is no frame to blur, so the
    fill is the fallback's (#70). Its 94% over a blur is in test_glass.py."""

    def test_nothing_makes_the_row_translucent_as_a_whole(self):
        chooser = Chooser(parent=None)

        assert chooser.row.windowOpacity() == 1.0
        assert not isinstance(chooser.row.graphicsEffect(), QGraphicsOpacityEffect)
        assert isinstance(chooser.tab.graphicsEffect(), QGraphicsOpacityEffect)

    def test_its_ground_is_the_fallbacks_and_its_controls_opaque(self):
        chooser = Chooser(parent=None)
        chooser.set_kind("record")
        # The shadow would darken the ground under it; the fill is the point.
        chooser.row.graphicsEffect().setEnabled(False)

        image = chooser.row.grab().toImage()

        ratio = image.devicePixelRatio()

        def at(point):
            return image.pixelColor(round(point.x() * ratio), round(point.y() * ratio))

        # In the gap between the kind well and the mode chip.
        gap = QPointF(
            METRIC.BORDER + METRIC.PAD
            + 2 * METRIC.WELL_PAD + 2 * METRIC.BTN + METRIC.WELL_GAP + METRIC.GAP / 2,
            METRIC.ROW_H / 2,
        )
        assert at(gap).alphaF() == pytest.approx(tokens.BarColor.FALLBACK_BG_ALPHA, abs=0.01)
        # The camcorder is hollow (bars/divergences.md 25), so the control's
        # opacity is its glyph's, not one pixel in the middle of it: the
        # stroke is fully opaque where it covers a pixel outright, over a
        # ground that never is.
        button = chooser.row.record
        corner = button.mapTo(chooser.row, QPoint(0, 0))
        alphas = [
            at(QPointF(corner.x() + x, corner.y() + y)).alphaF()
            for y in range(METRIC.BTN)
            for x in range(METRIC.BTN)
        ]
        assert max(alphas) == pytest.approx(1.0)
        assert _near(button.grab().toImage(), tokens.BarColor.REC_ON_FG)


class TestPlacement:
    """Centred on the monitor it is handed and flush to its top, in the host
    window's own coordinates. Which monitor is the overlay's call.

    Hosted, as the overlay hosts it: without a parent the row is a window of
    its own, and a platform may put a frame round a window's position.
    """

    @pytest.fixture(autouse=True)
    def _host(self):
        self.host = QWidget()
        self.host.resize(4480, 1440)
        yield
        self.host.deleteLater()

    def test_the_row_hangs_centred_from_the_top_with_the_pill_under_it(self):
        chooser = Chooser(self.host)

        chooser.set_screen(QRectF(0, 0, 1920, 1080), QPoint(0, 0))

        row = chooser.row.geometry()
        assert row.top() == 0
        assert abs(row.x() + row.width() / 2 - 960) <= 1
        assert chooser.hint.y() == METRIC.ROW_H + METRIC.HINT_GAP
        assert abs(chooser.hint.x() + chooser.hint.width() / 2 - 960) <= 1

    def test_on_a_monitor_with_a_negative_origin(self):
        # A monitor left of and above the primary: the host window's origin
        # is that monitor's top-left, so everything is placed relative to it.
        chooser = Chooser(self.host)
        origin = QPoint(-1920, -300)

        chooser.set_screen(QRectF(-1920, -300, 1920, 1080), origin)
        row = chooser.row.geometry()
        assert (row.top(), abs(row.x() + row.width() / 2 - 960) <= 1) == (0, True)

        chooser.set_screen(QRectF(0, 0, 2560, 1440), origin)
        row = chooser.row.geometry()
        assert row.top() == 300
        assert abs(row.x() + row.width() / 2 - (1920 + 1280)) <= 1

    def test_the_tab_hangs_from_the_same_edge(self):
        chooser = Chooser(self.host)
        chooser.set_screen(QRectF(-1920, -300, 1920, 1080), QPoint(-1920, -300))

        chooser.collapse()

        tab = chooser.tab.geometry()
        assert tab.top() == 0
        assert abs(tab.x() + tab.width() / 2 - 960) <= 1
        assert chooser.tab.isVisibleTo(self.host)
        assert not chooser.row.isVisibleTo(self.host)
        assert not chooser.hint.isVisibleTo(self.host)

    def test_the_row_stays_centred_as_it_changes_width(self):
        chooser = Chooser(self.host)
        chooser.set_screen(QRectF(0, 0, 1920, 1080), QPoint(0, 0))

        chooser.set_delay("10s")

        row = chooser.row.geometry()
        assert abs(row.x() + row.width() / 2 - 960) <= 1


class TestClickingTheChipClosesTheMenu:
    """Reported from a real Windows session: the mode menu could not be
    closed by clicking the chip that opened it -- it flickered and came
    straight back.

    The popup holds the mouse, so the dismissing press never reaches the
    chip while the menu is up. Qt closes the popup first and the chip sees
    the press afterwards, by which time the chooser has already forgotten
    the menu was open -- so the click reads as "open it", not "close it".
    That order is what these tests reproduce: `closed` fires, and only then
    does the chip's click arrive.
    """

    @staticmethod
    def _over_the_chip(monkeypatch, chooser):
        chip = chooser.row.mode_chip
        centre = chip.mapToGlobal(QPoint(chip.width() // 2, chip.height() // 2))
        monkeypatch.setattr(flowbars_module.QCursor, "pos", staticmethod(lambda: centre))

    def test_the_dismissing_press_does_not_reopen_it(self, monkeypatch):
        chooser = Chooser(parent=None)
        chooser._toggle_menu()
        assert chooser._menu is not None
        self._over_the_chip(monkeypatch, chooser)

        # What Qt does, in this order: the popup closes itself on the press,
        # then that same press is delivered to the chip underneath.
        chooser._on_menu_closed()
        chooser._toggle_menu()

        assert chooser._menu is None, "the menu reopened instead of staying closed"

    def test_a_later_click_on_the_same_chip_still_opens_it(self, monkeypatch):
        chooser = Chooser(parent=None)
        chooser._toggle_menu()
        self._over_the_chip(monkeypatch, chooser)
        chooser._on_menu_closed()
        # Past the guard's window: a deliberate second click, not the press
        # that closed the menu arriving late.
        monkeypatch.setattr(
            chooser._menu_guard, "_closed_at", time.monotonic() - 5.0, raising=False
        )

        chooser._toggle_menu()

        assert chooser._menu is not None

    def test_a_press_somewhere_else_does_not_block_the_chip(self, monkeypatch):
        chooser = Chooser(parent=None)
        chooser._toggle_menu()
        # Dismissed by a click well away from the chip -- on the frozen
        # frame, say. The next click on the chip is a real one.
        monkeypatch.setattr(
            flowbars_module.QCursor, "pos", staticmethod(lambda: QPoint(4000, 4000))
        )
        chooser._on_menu_closed()

        chooser._toggle_menu()

        assert chooser._menu is not None


class TestRecordingThePointer:
    """The record side's own flag. It was a Settings switch only, which is
    the home docs/design/pre-snip-chooser.md already rejected once for the
    Last-region preference: "a preference nobody finds is a preference
    nobody has". The row is where the decision is being made.
    """

    def test_it_is_only_on_the_record_side(self):
        chooser = Chooser(parent=None)

        assert not chooser.row.cursor_flag.isVisibleTo(chooser.row)

        chooser.set_kind("record")

        assert chooser.row.cursor_flag.isVisibleTo(chooser.row)

    def test_hide_sensitive_is_the_stills_sides_equivalent(self):
        # The two never show at once: one asks about a frozen frame, the
        # other about a moving one.
        chooser = Chooser(parent=None)
        chooser.set_kind("record")

        assert not chooser.row.hide_flag.isVisibleTo(chooser.row)

    def test_clicking_it_reports_the_new_state(self):
        chooser = Chooser(parent=None)
        chooser.set_kind("record")
        chooser.set_record_cursor(True)
        reported = []
        chooser.recordCursorChanged.connect(reported.append)

        _click(chooser.row.cursor_flag)

        assert reported == [False]
        assert chooser.record_cursor is False

    def test_seeding_it_never_reports(self):
        # Being told what the stored value is is not a change to it -- the
        # same split every other control on this row keeps.
        chooser = Chooser(parent=None)
        reported = []
        chooser.recordCursorChanged.connect(reported.append)

        chooser.set_record_cursor(True)

        assert reported == []
        assert chooser.record_cursor is True

    def test_where_the_platform_cannot_honour_it_the_flag_is_greyed(self):
        chooser = Chooser(parent=None)
        chooser.set_kind("record")

        chooser.set_record_cursor_available(False, "Windows records whatever it shows")

        assert not chooser.row.cursor_flag.is_available()

    def test_a_greyed_flag_refuses_the_click_rather_than_lying(self):
        chooser = Chooser(parent=None)
        chooser.set_kind("record")
        chooser.set_record_cursor(True)
        chooser.set_record_cursor_available(False, "Windows records whatever it shows")
        reported = []
        chooser.recordCursorChanged.connect(reported.append)

        _click(chooser.row.cursor_flag)

        assert reported == []
        assert chooser.record_cursor is True

    def test_the_hint_pill_carries_the_reason_it_is_greyed(self):
        # The handoff's rule: an option that cannot work says why. A switch
        # that silently does nothing is what Settings had on Windows.
        chooser = Chooser(parent=None)
        chooser.set_kind("record")
        chooser.set_record_cursor_available(False, "Windows records whatever it shows")

        _hover(chooser.row.cursor_flag)

        assert "Windows records whatever it shows" in chooser.hint.text

    def test_the_hint_pill_says_what_a_click_would_do(self):
        chooser = Chooser(parent=None)
        chooser.set_kind("record")
        chooser.set_record_cursor(True)

        _hover(chooser.row.cursor_flag)

        assert chooser.hint.text == tokens.RECORD_CURSOR_HINT[True]
