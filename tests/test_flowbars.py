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
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication

from snipux import design
from snipux.design import tokens
from snipux.flowbars import CountdownNumeral, RecordingBar


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
            assert bar.grab().height() == tokens.FlowMetric.ROW_H, bar.state()

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
        assert bar._action._label == "Record"
        bar.set_live("0:12")
        assert bar._action._label == "Stop"
        bar.set_done("00:27", destination="Save")
        assert bar._action._label == "Save"

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
            assert numeral.grab().width() == tokens.FlowMetric.COUNT_D
            assert numeral.grab().height() == tokens.FlowMetric.COUNT_D
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
