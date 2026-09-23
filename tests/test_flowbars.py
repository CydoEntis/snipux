"""The capture flow's recording bar, stages 3b-6 of docs/design/flow.

View only -- nothing here is wired to a recorder, and nothing in this file
should call into `recording.py` or any platform registry. What it covers:
the four states, that each names its own action, that the chrome between
groups appears only when it separates something, and the two controls the
handoff specifies but divergences.md defers.

`grab()` runs a full `paintEvent` into an offscreen pixmap without showing
anything, which is CLAUDE.md's preferred way to test painting code and the
only way to assert on it under QT_QPA_PLATFORM=offscreen.
"""

import pytest
from PyQt6.QtCore import QPoint, QRect, QRectF, Qt
from PyQt6.QtGui import QImage, QRegion
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QWidget

from snipux import design
from snipux.design import tokens
from snipux.flowbars import CountdownNumeral, FlowMenu, RecordingBar


@pytest.fixture(scope="module", autouse=True)
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


@pytest.fixture
def bar():
    widget = RecordingBar()
    yield widget
    widget.close()


def _click(widget):
    QTest.mouseClick(
        widget,
        Qt.MouseButton.LeftButton,
        pos=QPoint(widget.width() // 2, widget.height() // 2),
    )


class TestTheFourStates:
    def test_every_state_is_exactly_one_row_tall(self, bar):
        # ROW_H is "6 pad + 28 control + 6 pad + 2x1px border". The border's
        # pixel is easy to leave out of the margins, which gives a 40px bar
        # against a 42px token and makes every placement measured from it
        # two pixels wrong.
        for setup in (
            bar.set_ready,
            lambda: bar.set_counting(3),
            lambda: bar.set_live("0:12", size="1280x720"),
            lambda: bar.set_done("00:27 - 11.3 MB - webm"),
        ):
            setup()
            # `deviceIndependentSize()`, not `height()`: grab() hands back
            # PHYSICAL pixels, so at 1.5x scaling a correct 42px bar
            # measures 63 and the token comparison fails on a machine whose
            # display is scaled rather than on a bug.
            height = bar.grab().deviceIndependentSize().height()
            assert round(height) == tokens.FlowMetric.ROW_H, bar.state()

    def test_each_state_reports_itself(self, bar):
        bar.set_ready()
        assert bar.state() == RecordingBar.READY
        bar.set_counting(3)
        assert bar.state() == RecordingBar.COUNTING
        bar.set_live("0:01")
        assert bar.state() == RecordingBar.LIVE
        bar.set_done("00:27")
        assert bar.state() == RecordingBar.DONE

    def test_the_action_always_names_what_pressing_it_does(self, bar):
        # The pill this replaces was a bare elapsed-time readout whose whole
        # surface silently stopped the recording. Naming the action is the
        # point of the shape; a state that shows only a number is the bug.
        bar.set_ready()
        assert bar._action._label.startswith("Record")
        bar.set_live("0:12")
        assert bar._action._label == "Stop"
        bar.set_done("00:27", destination="Save")
        assert bar._action._label == "Save"

    def test_the_glyph_follows_the_action_not_just_the_label(self, bar):
        # A filled circle beside the word "Stop" reads as record whatever
        # the label says -- the exact shape of mistake this redesign exists
        # to stop making, so the glyph changes with the action.
        bar.set_ready()
        assert bar._action._glyph == "circle"
        bar.set_live("0:12")
        assert bar._action._glyph == "square"
        # `done` hides the action entirely unless a destination is given,
        # so the glyph is only asked about when there is a button wearing it.
        bar.set_done("00:27", destination="Copy")
        assert bar._action._glyph is None

    def test_counting_says_how_long_is_left_without_offering_an_action(self, bar):
        bar.set_counting(2)

        assert "2" in bar._action._label
        # Cancel is its own control rather than a second meaning for the
        # action, so pressing the armed pill mid-countdown does nothing.
        fired = []
        bar.startClicked.connect(lambda: fired.append("start"))
        bar.stopClicked.connect(lambda: fired.append("stop"))
        bar._on_action()
        assert fired == []


class TestWhatEachStateShows:
    def test_ready_offers_audio_and_delay_but_no_clock(self, bar):
        # `isHidden()` rather than `isVisible()`: a child of a window that
        # was never shown is not visible whatever its own flag says, so
        # isVisible() would be False for every control here and the test
        # would pass without checking anything.
        bar.set_ready()

        assert bar._audio.isHidden() is False
        assert bar._delay.isHidden() is False
        assert bar._clock.isHidden() is True
        assert bar._summary.isHidden() is True

    def test_live_shows_the_clock_and_drops_the_delay(self, bar):
        bar.set_live("0:12", size="1280x720")

        assert bar._clock.isHidden() is False
        assert bar._delay.isHidden() is True
        assert bar._clock.text() == "0:12"

    def test_pause_is_only_offered_while_live(self, bar):
        bar.set_ready()
        assert bar._pause.isHidden() is True
        bar.set_counting(3)
        assert bar._pause.isHidden() is True
        bar.set_live("0:12")
        assert bar._pause.isHidden() is False
        bar.set_done("00:27")
        assert bar._pause.isHidden() is True

    def test_delay_is_gone_once_it_is_rolling(self, bar):
        # A countdown control during a recording is a control that cannot
        # act -- the same "a control that opens a menu it can't act on is a
        # lie" rule the handoff applies to the mode chip.
        bar.set_live("0:01")
        assert bar._delay.isHidden() is True
        bar.set_done("00:27")
        assert bar._delay.isHidden() is True

    def test_done_shows_the_summary_and_a_way_to_throw_it_away(self, bar):
        bar.set_done("00:27 - 11.3 MB - webm")

        assert bar._summary.text() == "00:27 - 11.3 MB - webm"
        assert bar._discard.isHidden() is False
        assert bar._cancel.isHidden() is True

    def test_done_shows_no_action_unless_a_destination_is_given(self, bar):
        # The file has already landed by then, so an accent button reading
        # "Copy" would be a control with nothing to do -- the exact thing
        # this design removes everywhere else.
        bar.set_done("00:27 - 11.3 MB - webm")
        assert bar._action.isHidden() is True

        bar.set_done("00:27 - 11.3 MB - webm", destination="Copy")
        assert bar._action.isHidden() is False

    def test_a_divider_only_appears_when_it_separates_something(self, bar):
        # Two dividers with nothing between them is how a bar looks broken
        # in one state and fine in every other.
        bar.set_counting(3)
        assert bar._tail_divider.isHidden() is False  # action | cancel

        bar.set_live("0:12")
        # Nothing after audio in the live state, so the tail divider would
        # sit at the bar's right edge with nothing beyond it.
        assert bar._tail_divider.isHidden() is True


class TestTheLiveStateIsTheOnlyRedOne:
    def _border_pixels(self, bar):
        image = bar.grab().toImage()
        ratio = image.devicePixelRatio()
        mid_y = round((bar.height() / 2) * ratio)
        return image.pixelColor(round(0.5 * ratio), mid_y)

    def test_live_paints_a_red_hairline_and_the_others_do_not(self, bar):
        bar.set_ready()
        idle = self._border_pixels(bar)
        bar.set_live("0:12")
        live = self._border_pixels(bar)

        assert live != idle
        # Red is the whole signal: it appears nowhere else in the product,
        # which is what lets the border say "recording" without a label.
        # Asserted as dominance rather than proximity to the raw token --
        # the hairline is 34% alpha over the glass, so it composites well
        # short of #ff5a52 and a distance threshold would only be measuring
        # the fill behind it.
        assert live.red() > live.green() and live.red() > live.blue()
        assert live.red() > idle.red()
        assert idle.red() <= idle.green()  # the idle hairline is neutral


class TestAudio:
    def test_the_glyph_follows_the_chosen_source(self, bar):
        for identifier, icon_name, _label, _note in tokens.AUDIO_SOURCES:
            bar.set_audio(identifier)
            assert bar._audio._icon_name == icon_name

    def test_an_unknown_source_falls_back_to_muted(self, bar):
        bar.set_audio("something-else")

        assert bar._audio._icon_name == "mute"

    def test_a_disabled_control_stays_visible_and_inert(self, bar):
        # docs/design/flow/divergences.md 2: GNOME's screencast has no audio
        # at all, and the handoff's own rule is that an option which cannot
        # work is shown with the reason rather than hidden. Hiding it is the
        # same lie told quietly.
        bar.set_ready()
        bar.set_audio_enabled(False)
        fired = []
        bar.audioClicked.connect(lambda: fired.append(True))

        _click(bar._audio)

        assert bar._audio.isHidden() is False
        assert fired == []

    def test_an_enabled_control_reports_its_click(self, bar):
        bar.set_ready()
        bar.set_audio_enabled(True)
        fired = []
        bar.audioClicked.connect(lambda: fired.append(True))

        _click(bar._audio)

        assert fired == [True]


class TestPause:
    """#87: whether a click on this control does anything is
    `RecordingBackend.can_pause`, answered by `app.py` and applied here
    through `set_pause_enabled` -- the same "the bar renders it, the
    caller decides it" split `TestAudio` already covers for GNOME's
    missing audio route.
    """

    def test_live_names_pause_beside_stop(self, bar):
        bar.set_live("0:12")

        assert bar._pause._label == "Pause"
        assert bar._action._label == "Stop"

    def test_a_disabled_control_stays_visible_and_inert(self, bar):
        bar.set_live("0:12")
        bar.set_pause_enabled(False)
        fired = []
        bar.pauseClicked.connect(lambda: fired.append(True))

        _click(bar._pause)

        assert bar._pause.isHidden() is False
        assert fired == []

    def test_an_enabled_control_reports_its_click(self, bar):
        bar.set_live("0:12")
        bar.set_pause_enabled(True)
        fired = []
        bar.pauseClicked.connect(lambda: fired.append(True))

        _click(bar._pause)

        assert fired == [True]

    def test_pause_control_is_handed_out_for_a_tooltip(self, bar):
        assert bar.pause_control() is bar._pause

    def test_set_paused_names_resume_and_freezes_the_clock(self, bar):
        bar.set_live("0:12")

        bar.set_paused("0:12", "0:03")

        assert bar._pause._label == "Resume · 0:03"
        assert bar._clock.text() == "0:12"
        # Still the live state -- Stop and Discard work exactly as they do
        # while recording, per the class docstring.
        assert bar.state() == RecordingBar.LIVE

    def test_set_live_resumes_the_control_back_to_pause(self, bar):
        # live -> paused -> live, and as many times as the user pauses.
        bar.set_live("0:12")
        bar.set_paused("0:12", "0:03")

        bar.set_live("0:15")

        assert bar._pause._label == "Pause"


class TestTheDestinationChip:
    """What Stop will do with the recording is its own control beside
    Record, not a caret on it: a press on Record always records."""

    @pytest.mark.parametrize("identifier, glyph, label", [
        (identifier, glyph, label)
        for identifier, glyph, label, _note in tokens.RECORD_DESTINATIONS
    ])
    def test_the_chip_names_the_destination(self, bar, identifier, glyph, label):
        bar.set_destination(identifier)
        bar.set_ready()

        assert bar._destination_chip.label() == label
        assert bar._destination_chip._icon_name == glyph
        assert bar.destination() == identifier

    def test_record_says_record_whatever_the_destination(self, bar):
        bar.set_ready()
        bar.set_destination("gif")

        assert bar._action._label == "Record"

    def test_an_unknown_destination_falls_back_to_the_default(self, bar):
        bar.set_destination("review")  # a stills destination, not a recording's

        assert bar.destination() == tokens.RECORD_AFTER_DEFAULT

    def test_the_chip_asks_for_the_menu_and_record_starts(self, bar):
        bar.set_ready()
        fired = []
        bar.startClicked.connect(lambda: fired.append("start"))
        bar.destinationMenuRequested.connect(lambda: fired.append("menu"))

        _click(bar._destination_chip)
        _click(bar._action)

        assert fired == ["menu", "start"]

    def test_the_chip_sits_right_after_the_action_group(self, bar):
        # Rule 3: the action group, a divider, then everything else -- and
        # the destination is the first of everything else.
        bar.set_ready()
        bar.adjustSize()

        assert bar._action.geometry().right() < bar._action_divider.geometry().left()
        assert bar._action_divider.geometry().right() < bar._destination_chip.geometry().left()
        assert bar._destination_chip.geometry().right() < bar._audio.geometry().left()

    def test_only_the_ready_stage_offers_it(self, bar):
        bar.set_ready()
        assert bar._destination_chip.isHidden() is False
        bar.set_counting(3)
        assert bar._destination_chip.isHidden() is True
        bar.set_live("0:01")
        assert bar._destination_chip.isHidden() is True
        bar.set_done("00:27")
        assert bar._destination_chip.isHidden() is True

    def test_record_names_its_key(self, bar):
        # No inline "↵", as the stills split carries none; Enter still
        # starts it, and the tooltip is where that is said.
        bar.set_ready()

        assert "Enter" in bar._action.toolTip()


class TestTheDelayShowsWhatIsSet:
    def test_no_delay_is_the_bare_glyph(self, bar):
        bar.set_ready()
        bar.set_delay(tokens.DELAYS[0])

        assert bar._delay.value() == ""
        assert bar._delay.width() == tokens.FlowMetric.BTN

    def test_a_set_delay_is_on_the_bar(self, bar):
        # A countdown carried over from the chooser used to be invisible
        # here, until Record was pressed and nothing happened.
        bar.set_ready()

        bar.set_delay("10s")

        assert bar._delay.value() == "10s"
        assert bar._delay.width() > tokens.FlowMetric.BTN


class TestTheLiveAudioIsAReadout:
    def test_a_live_audio_click_opens_nothing(self, bar):
        # The source is read when the recorder starts; a menu mid-recording
        # would change nothing that is being recorded.
        bar.set_live("0:12")
        bar.set_audio_enabled(True)
        fired = []
        bar.audioClicked.connect(lambda: fired.append(True))

        _click(bar._audio)

        assert bar._audio.isHidden() is False
        assert fired == []

    def test_ready_again_makes_it_a_control_again(self, bar):
        bar.set_live("0:12")
        bar.set_ready()
        bar.set_audio_enabled(True)
        fired = []
        bar.audioClicked.connect(lambda: fired.append(True))

        _click(bar._audio)

        assert fired == [True]


class TestTheBarsOwnChrome:
    def test_the_countdown_does_not_draw_two_dividers_side_by_side(self, bar):
        bar.set_counting(3)

        shown = [
            divider for divider in (bar._action_divider, bar._tail_divider)
            if not divider.isHidden()
        ]

        assert len(shown) == 1

    def test_with_nothing_to_blur_the_fill_hides_what_is_behind_it(self, bar):
        # 93% let a window title on the desktop show through the bar as a
        # ghost of some other control. With no frozen frame to blur, the
        # fill is the glass fallback's instead.
        bar.set_ready()
        bar.adjustSize()
        image = QImage(bar.size(), QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(Qt.GlobalColor.transparent)
        bar.render(image, QPoint(), QRegion(), QWidget.RenderFlag.DrawWindowBackground)

        # A point of bare fill: past the Record button, on the bar's padding.
        alpha = image.pixelColor(bar._action.geometry().left() - 3, bar.height() // 2).alphaF()

        assert alpha >= tokens.BarColor.FALLBACK_BG_ALPHA - 0.01


class TestTheActionRoutesByState:
    def test_ready_starts_live_stops_done_fires_the_destination(self, bar):
        fired = []
        bar.startClicked.connect(lambda: fired.append("start"))
        bar.stopClicked.connect(lambda: fired.append("stop"))
        bar.destinationClicked.connect(lambda: fired.append("destination"))

        bar.set_ready()
        bar._on_action()
        bar.set_live("0:12")
        bar._on_action()
        bar.set_done("00:27")
        bar._on_action()

        assert fired == ["start", "stop", "destination"]


class TestCountdownNumeral:
    def test_it_is_the_token_diameter(self):
        numeral = CountdownNumeral()
        try:
            # Logical pixels -- see the note in
            # TestTheFourStates.test_every_state_is_exactly_one_row_tall.
            size = numeral.grab().deviceIndependentSize()
            assert round(size.width()) == tokens.FlowMetric.COUNT_D
            assert round(size.height()) == tokens.FlowMetric.COUNT_D
        finally:
            numeral.close()

    def test_it_centres_on_the_region_it_is_given(self):
        # Inside the region, not on the bar: it goes where the user is
        # already looking. The old build put the count on the pill, and the
        # opening seconds of every recording were still of somebody looking
        # away from the frame.
        numeral = CountdownNumeral()
        try:
            region = QRect(400, 300, 600, 400)
            numeral.show_centered_on(region)

            # QRectF's centre, not QRect's: QRect.center() floors to the
            # pixel left of true centre (699 for a 600-wide rect at x=400),
            # and a countdown that sits half a pixel off the region it is
            # announcing is not worth inheriting that convention for.
            centre = QRectF(region).center()
            half = tokens.FlowMetric.COUNT_D / 2
            assert numeral.x() == round(centre.x() - half)
            assert numeral.y() == round(centre.y() - half)
        finally:
            numeral.close()

    def test_the_numeral_it_paints_is_the_one_it_was_given(self):
        numeral = CountdownNumeral()
        try:
            numeral.set_seconds(3)
            three = numeral.grab().toImage()
            numeral.set_seconds(1)
            one = numeral.grab().toImage()

            assert three != one
        finally:
            numeral.close()


class TestFlowMenu:
    """The dropdowns. A top-level popup rather than a child of the bar,
    because an open menu has to paint above the hint pill *below* the bar
    and an effect-bearing parent traps it in its own stacking context --
    the HTML reference hit exactly this with `backdrop-filter`.
    """

    def _rows(self, disabled_reason=""):
        return [
            ("system", "System", "Desktop output", "", disabled_reason),
            ("mic", "Mic", "Default input", "", disabled_reason),
            ("off", "Muted", "No audio track at all", "", ""),
        ]

    def test_it_is_a_popup_not_a_child(self):
        menu = FlowMenu(self._rows(), "off", 250)
        try:
            assert menu.parent() is None
            assert bool(menu.windowFlags() & Qt.WindowType.Popup)
        finally:
            menu.close()

    def test_choosing_a_row_reports_its_value_and_closes(self):
        menu = FlowMenu(self._rows(), "off", 250)
        try:
            chosen = []
            menu.chosen.connect(chosen.append)

            row_h = menu._row_height()
            y = tokens.FlowMetric.MENU_PAD + row_h + row_h / 2  # the second row
            QTest.mouseClick(menu, Qt.MouseButton.LeftButton, pos=QPoint(40, int(y)))

            assert chosen == ["mic"]
        finally:
            menu.close()

    def test_a_disabled_row_refuses_to_be_chosen(self):
        # The handoff's rule is that an option which cannot work says why
        # rather than vanishing -- but saying why and then accepting the
        # click would be worse than either.
        menu = FlowMenu(self._rows("Not available on Linux"), "off", 250)
        try:
            chosen = []
            menu.chosen.connect(chosen.append)

            row_h = menu._row_height()
            y = tokens.FlowMetric.MENU_PAD + row_h / 2  # the first, disabled row
            QTest.mouseClick(menu, Qt.MouseButton.LeftButton, pos=QPoint(40, int(y)))

            assert chosen == []
        finally:
            menu.close()

    def test_a_click_outside_any_row_chooses_nothing(self):
        menu = FlowMenu(self._rows(), "off", 250)
        try:
            chosen = []
            menu.chosen.connect(chosen.append)

            QTest.mouseClick(menu, Qt.MouseButton.LeftButton, pos=QPoint(40, 1))

            assert chosen == []
        finally:
            menu.close()

    def test_opening_upward_puts_it_clear_of_the_anchor(self):
        # Audio opens upward so it never covers the region being recorded,
        # which is the one thing on screen the user is trying to look at.
        menu = FlowMenu(self._rows(), "off", 250)
        try:
            anchor = QRect(500, 700, 28, 28)
            menu.open_above(anchor)

            assert menu.y() + menu.height() <= anchor.top()
        finally:
            menu.close()

    def test_opening_downward_puts_it_clear_the_other_way(self):
        menu = FlowMenu(self._rows(), "off", 250)
        try:
            anchor = QRect(500, 100, 28, 28)
            menu.open_below(anchor)

            assert menu.y() >= anchor.bottom()
        finally:
            menu.close()


class TestAMenuNeverLeavesTheScreen:
    """Reported with a screenshot: the recording delay menu opened downward
    from a bar low on the screen, so "3s" and "5s" ran off the bottom edge
    and under the taskbar, where they could not be clicked.

    `within` is passed explicitly here rather than left to
    `QGuiApplication.screenAt`: the offscreen platform's screen is not the
    one the assertion is about, and a placement test that depends on the
    runner's monitor answers differently on every machine.
    """

    SCREEN = QRect(0, 0, 1920, 1040)

    def _menu(self):
        rows = [(value, value, "", "", "") for value in ("No delay", "3s", "5s", "10s")]
        menu = FlowMenu(rows, "No delay", 160)
        menu.adjustSize()
        return menu

    def test_it_flips_above_a_control_near_the_bottom(self):
        menu = self._menu()
        # A bar sitting just above the taskbar: there is no room under it.
        anchor = QRect(900, 1000, 90, 32)

        menu.open_below(anchor, self.SCREEN)

        assert menu.geometry().bottom() <= self.SCREEN.bottom()
        assert menu.geometry().bottom() <= anchor.top()

    def test_it_flips_below_a_control_near_the_top(self):
        menu = self._menu()
        # The recording bar's own home: 12px under the top of the screen.
        anchor = QRect(900, 12, 90, 32)

        menu.open_above(anchor, self.SCREEN)

        assert menu.geometry().top() >= self.SCREEN.top()
        assert menu.geometry().top() >= anchor.bottom()

    def test_it_stays_on_screen_at_the_right_edge(self):
        menu = self._menu()
        anchor = QRect(self.SCREEN.right() - 40, 400, 32, 32)

        menu.open_below(anchor, self.SCREEN)

        assert menu.geometry().right() <= self.SCREEN.right()
        assert menu.geometry().left() >= self.SCREEN.left()

    def test_it_stays_on_screen_at_the_left_edge(self):
        menu = self._menu()
        anchor = QRect(self.SCREEN.left() + 4, 400, 32, 32)

        menu.open_below(anchor, self.SCREEN)

        assert menu.geometry().left() >= self.SCREEN.left()

    def test_it_still_opens_where_asked_when_there_is_room(self):
        menu = self._menu()
        anchor = QRect(900, 500, 90, 32)

        menu.open_below(anchor, self.SCREEN)

        assert menu.geometry().top() >= anchor.bottom()
