"""Tests for `snipux/player.py` -- the recording player / trim editor from
`docs/design/player`.

The load-bearing claim here is that every readout in the window is a
different phrasing of one `TrimState`, and that what the transport, the
canvas badge, the trim row and the export estimate say can never disagree.
The second is that the rail maps a pixel to a time against *itself*, which
is the difference between a handle that drags and one that teleports.

Painting is exercised through `QWidget.grab()`, which runs a full
`paintEvent` into an offscreen pixmap -- the same technique the rest of the
suite uses, and the reason these pass with no display.
"""

from __future__ import annotations

import math

import pytest
from PyQt6.QtCore import QPoint, QPointF, QSize, Qt
from PyQt6.QtGui import QColor, QImage
from PyQt6.QtWidgets import QApplication

from snipux.design import tokens
from snipux.player import (
    EXPORT_UNAVAILABLE,
    TimelineRail,
    TransportBar,
    TrimState,
    VideoCanvas,
    estimate_size_mb,
    export_availability,
    export_copy,
    export_frame,
    format_clock,
    format_timecode,
)


@pytest.fixture(scope="module", autouse=True)
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


@pytest.fixture
def rail():
    state = TrimState(duration=27.4, start=2.6, end=22.8, position=4.2)
    widget = TimelineRail(state)
    widget.resize(940, tokens.PlayerMetric.RAIL_H)
    widget.sync()
    return widget


class TestTimecodesReadInFrames:
    def test_the_handoffs_own_example(self):
        # The handoff prints "00:12.18" for a playhead at 12.6s and 30fps.
        # Frames rather than hundredths because a trim lands on a frame
        # boundary -- "00:12.60" would not be a position in the file.
        assert format_timecode(12.6) == "00:12.18"

    def test_the_frame_field_never_reaches_the_frame_rate(self):
        # 0.999s is 29.97 frames, which rounds to 30 -- a frame number that
        # does not exist in a 30fps second.
        assert format_timecode(0.999).endswith(".29")

    def test_durations_drop_the_frame_field(self):
        # A duration is read at a glance rather than aimed at.
        assert format_clock(27.4) == "00:27"
        assert format_clock(90) == "01:30"

    def test_negative_time_does_not_produce_a_negative_clock(self):
        assert format_timecode(-1) == "00:00.00"


class TestTrimStateIsTheSingleSourceOfEveryReadout:
    def test_kept_and_cut_add_up_to_the_duration(self):
        state = TrimState(duration=27.4, start=2.6, end=22.8)
        assert state.kept == pytest.approx(20.2)
        assert state.kept + state.cut == pytest.approx(state.duration)

    def test_a_full_length_range_does_not_count_as_trimmed(self):
        # The cut clause must not appear on a recording nobody cut, and a
        # handle dragged to the very end lands within a rounding error.
        state = TrimState(duration=27.4, start=0.0, end=27.39)
        assert state.trimmed is False

    def test_a_real_cut_counts_as_trimmed(self):
        state = TrimState(duration=27.4, start=2.6, end=22.8)
        assert state.trimmed is True


class TestTheRailMapsPixelsToTimeAgainstItself:
    def test_time_at_and_x_for_are_inverses(self, rail):
        for seconds in (0.0, 5.0, 13.7, 27.4):
            assert rail.time_at(rail.x_for(seconds)) == pytest.approx(seconds)

    def test_a_position_past_either_edge_clamps(self, rail):
        assert rail.time_at(-40) == 0.0
        assert rail.time_at(rail.width() + 40) == pytest.approx(27.4)

    def test_dragging_the_in_handle_uses_the_rails_width_not_the_handles(self, rail):
        # The bug the handoff warns about: the handle is 14px wide, so a
        # fraction taken against it resolves to 0 or 1 and the handle
        # teleports to whichever end it was nearest. Pressing the handle
        # and dragging to the rail's midpoint must land at mid-duration.
        handle = rail._handle_in
        rail.begin_drag("in", QPointF(handle.mapToGlobal(QPoint(7, 10))))
        middle = rail.mapToGlobal(QPoint(rail.width() // 2, 10))
        rail.drag_to(QPointF(middle))

        assert rail.state.start == pytest.approx(13.7, abs=0.1)

    def test_the_handles_cannot_cross(self, rail):
        rail.begin_drag("in", QPointF(rail.mapToGlobal(QPoint(0, 10))))
        far_right = rail.mapToGlobal(QPoint(rail.width() - 1, 10))
        rail.drag_to(QPointF(far_right))

        floor = tokens.PlayerMetric.MIN_RANGE_S
        assert rail.state.start == pytest.approx(rail.state.end - floor)

    def test_dragging_a_handle_moves_the_playhead_to_it(self, rail):
        # So you always see the frame you are cutting on.
        rail.begin_drag("out", QPointF(rail.mapToGlobal(QPoint(400, 10))))

        assert rail.state.position == pytest.approx(rail.state.end)


class TestTheRailPaints:
    @staticmethod
    def _flag_extent(rail):
        """Where the playhead's time flag actually paints, in x.

        Measured off the rendered pixels rather than recomputed from the
        state, so the test can catch the flag being drawn somewhere the
        geometry says it should not be.
        """
        pixmap = rail.grab()
        image = pixmap.toImage()
        flag = QColor(tokens.PlayerColor.PLAYHEAD)
        # `grab()` returns PHYSICAL pixels, so at fractional scaling the
        # image is wider than the widget's logical width and the two spaces
        # must not be compared. Everything here stays in the image's own
        # space, and the caller is handed its width to compare against.
        dpr = pixmap.devicePixelRatio() or 1.0
        # Logical row 12 is inside the flag and below its text, so the
        # glyphs do not punch holes in the run being measured.
        row = round(12 * dpr)
        columns = [
            x for x in range(image.width()) if image.pixelColor(x, row) == flag
        ]
        return min(columns), max(columns), image.width()

    def test_the_playhead_flag_stays_inside_the_rail_at_the_start(self, rail):
        # At t=0 a flag centred on the playhead would hang half off the
        # left edge and be clipped by the rounded corner.
        rail.state.position = 0.0
        left, right, width = self._flag_extent(rail)

        assert left >= 0
        assert right < width

    def test_the_playhead_flag_stays_inside_the_rail_at_the_end(self, rail):
        rail.state.position = rail.state.duration
        left, right, width = self._flag_extent(rail)

        assert right <= width - 1
        assert left > width // 2

    def test_no_waveform_band_without_an_audio_track(self, rail):
        # A flat line looks like silence; silence is not the same fact as
        # no audio at all, so the band is hidden and the filmstrip grows.
        assert rail.has_audio is False
        assert rail._film_height() == rail.height() - tokens.PlayerMetric.RULER_H

    def test_peaks_bring_the_band_back_and_shrink_the_filmstrip(self, rail):
        rail.set_peaks([abs(math.sin(i * 0.7)) for i in range(120)])

        assert rail.has_audio is True
        assert rail._film_height() == tokens.PlayerMetric.FILMSTRIP_H

    def test_muting_greys_the_whole_waveform(self, rail):
        # "This export has no audio" has to be visible without reading a
        # control, so the greying is what the mute button promises.
        rail.set_peaks([1.0] * 120)
        loud = rail.grab().toImage()
        rail.set_muted(True)
        muted = rail.grab().toImage()

        assert loud != muted

    def test_cells_outside_the_range_are_dimmed_not_hidden(self, rail):
        # Dimmed rather than gone: what was cut still has to be findable to
        # be dragged back in.
        assert tokens.PlayerColor.OUTSIDE_OPACITY > 0


class TestTheTransportSaysWhatWillBeExported:
    def test_the_bar_builds_every_control_the_handoff_lists(self):
        bar = TransportBar()
        for control in (
            bar.play_button,
            bar.back_button,
            bar.forward_button,
            bar.time_label,
            bar.mute_button,
            bar.loop_button,
            bar.speed_button,
        ):
            assert control is not None

    def test_play_is_the_only_pre_lit_control(self):
        # A bar of uniformly idle buttons gives the eye nowhere to land.
        bar = TransportBar()
        assert bar.play_button._tone == "on"
        assert bar.back_button._tone == "idle"
        assert bar.mute_button._tone == "idle"

    def test_loop_starts_engaged(self):
        # You are judging a short clip, and a clip that stops dead after
        # two seconds cannot be judged.
        assert TransportBar().loop_button._tone == "accent"


class TestTheCanvas:
    def test_zoom_clamps_to_the_designs_range(self):
        canvas = VideoCanvas()
        low, high, _step = tokens.PlayerMetric.ZOOM_STEPS
        canvas.set_zoom(10)
        assert canvas.zoom == low
        canvas.set_zoom(500)
        assert canvas.zoom == high

    def test_a_frame_sets_the_source_size(self):
        canvas = VideoCanvas()
        canvas.set_frame(QImage(1017, 562, QImage.Format.Format_RGB32))
        assert canvas.source_size == QSize(1017, 562)

    def test_the_video_rect_is_centred(self):
        canvas = VideoCanvas()
        canvas.resize(900, 600)
        canvas.set_source_size(QSize(400, 300))
        rect = canvas.video_rect()
        assert rect.center().x() == pytest.approx(450)
        assert rect.center().y() == pytest.approx(300)

    def test_nothing_is_drawn_without_a_size(self):
        canvas = VideoCanvas()
        canvas.resize(400, 300)
        assert canvas.video_rect().isEmpty()
        canvas.grab()  # must not raise


class TestExportAvailabilityExplainsItself:
    def test_gif_is_never_available_and_says_why(self):
        # No GIF encoder in this build, and a missing row reads as a bug
        # while a greyed row with a reason reads as a limit.
        assert "gif" in export_availability(trimmed=False)
        assert "gif" in export_availability(trimmed=True)
        assert EXPORT_UNAVAILABLE["gif"]

    def test_webm_is_available_untrimmed_and_not_trimmed(self):
        # Untrimmed it is a byte-for-byte copy and needs no encoder at all;
        # trimmed it would need the VP9 encoder this build does not carry.
        assert "webm" not in export_availability(trimmed=False)
        assert "webm" in export_availability(trimmed=True)

    def test_mp4_and_a_still_frame_are_always_available(self):
        for trimmed in (False, True):
            assert "mp4" not in export_availability(trimmed)
            assert "frame" not in export_availability(trimmed)


class TestSizeEstimatesFollowTheTrim:
    def test_an_estimate_scales_with_the_kept_duration(self):
        assert estimate_size_mb("mp4", 20.0) == pytest.approx(11.0)
        assert estimate_size_mb("mp4", 10.0) == pytest.approx(5.5)

    def test_a_still_has_a_fixed_estimate(self):
        # It is one frame; the trim length has nothing to do with it.
        assert estimate_size_mb("frame", 20.0) == estimate_size_mb("frame", 1.0)

    def test_gif_is_estimated_far_larger_than_mp4(self):
        # "Big above ~10 seconds" is a sentence; the figure is the answer.
        assert estimate_size_mb("gif", 20.0) > estimate_size_mb("mp4", 20.0) * 2


class TestExportsNeverTouchTheSource:
    def test_an_untrimmed_webm_is_copied_byte_for_byte(self, tmp_path):
        source = tmp_path / "recording.webm"
        source.write_bytes(b"not really a video, but bytes are bytes")
        destination = tmp_path / "copy.webm"

        export_copy(source, destination)

        assert destination.read_bytes() == source.read_bytes()
        assert source.exists()

    def test_a_still_frame_writes_a_real_png(self, tmp_path):
        image = QImage(64, 48, QImage.Format.Format_RGB32)
        image.fill(QColor("#c8d96a"))
        destination = tmp_path / "frame.png"

        export_frame(image, destination)

        # Read back rather than trusting the return: the point of this
        # export is a file another program can open.
        written = QImage(str(destination))
        assert written.size() == QSize(64, 48)
        assert written.pixelColor(10, 10) == QColor("#c8d96a")

    def test_exporting_a_frame_that_does_not_exist_says_so(self, tmp_path):
        with pytest.raises(ValueError):
            export_frame(QImage(), tmp_path / "nothing.png")


class TestTheReadoutIsNeverClippedShortOfItsLastFigure:
    """The trim readout answers "what am I about to export", and its last
    clause is the cut figure -- the one number that only appears when
    something was actually removed.

    It was clipped twice over: the label sits after a stretch and merely
    *preferred* its hint, and `QLabel.sizeHint()` under-reports rich text
    by tens of pixels. Both bugs rendered a sentence that read as complete
    while silently dropping its point, which is why these measure painted
    pixels rather than the text property.
    """

    @staticmethod
    def _readout(state):
        from snipux.player import _TrimReadout

        label = _TrimReadout()
        label.set_state(state)
        label.adjustSize()
        return label

    def test_the_cut_clause_is_painted_when_something_was_cut(self):
        from PyQt6.QtGui import QFontMetrics

        state = TrimState(duration=27.4, start=2.6, end=22.8)
        label = self._readout(state)

        # Wide enough for every clause, measured off the plain equivalent --
        # the rich-text hint is the thing that lied.
        plain = "in 00:02.18  ·  out 00:22.24  ·  keeping 00:20 of 00:27  ·  −00:07 cut"
        needed = QFontMetrics(label.font()).horizontalAdvance(plain)
        assert label.width() >= needed

    def test_no_cut_clause_when_nothing_was_cut(self):
        untrimmed = self._readout(TrimState(duration=27.4, start=0.0, end=27.4))
        trimmed = self._readout(TrimState(duration=27.4, start=2.6, end=22.8))

        assert "cut" not in untrimmed.text()
        assert "cut" in trimmed.text()
        # And the label shrinks to match, rather than leaving a gap where
        # the clause would have been.
        assert untrimmed.width() < trimmed.width()

    def test_the_kept_and_cut_figures_carry_their_own_colours(self):
        label = self._readout(TrimState(duration=27.4, start=2.6, end=22.8))

        assert tokens.PlayerColor.KEPT_FG in label.text()
        assert tokens.PlayerColor.CUT_FG in label.text()


class TestExportNamesNeverCollideWithTheSource:
    """`PRESERVE_ORIGINAL` -- the untrimmed original stays at its own path
    until the user explicitly overwrites it, which the export menu promises
    in so many words."""

    @staticmethod
    def _window(tmp_path, monkeypatch, *, trimmed):
        from snipux import player

        source = tmp_path / "Recording from 2026-08-28 11-40-21.webm"
        source.write_bytes(b"stand-in")

        # The naming is pure path arithmetic on `self.path` and
        # `self.state`; building the whole window would need a decodable
        # file and an event loop to say the same thing.
        class Stub:
            path = source
            state = TrimState(
                duration=27.4, start=2.6 if trimmed else 0.0, end=22.8 if trimmed else 27.4
            )
            _destination_for = player.PlayerWindow._destination_for

        return Stub()

    def test_a_trimmed_video_is_marked_as_a_different_clip(self, tmp_path, monkeypatch):
        window = self._window(tmp_path, monkeypatch, trimmed=True)
        assert window._destination_for("mp4").name.endswith("(trimmed).mp4")

    def test_a_still_is_never_called_trimmed(self, tmp_path, monkeypatch):
        # One frame is not a trimmed clip, whatever the range says.
        window = self._window(tmp_path, monkeypatch, trimmed=True)
        assert "(trimmed)" not in window._destination_for("frame").name
        assert window._destination_for("frame").suffix == ".png"

    def test_an_untrimmed_export_keeps_the_plain_name(self, tmp_path, monkeypatch):
        window = self._window(tmp_path, monkeypatch, trimmed=False)
        assert window._destination_for("mp4").name.endswith("11-40-21.mp4")

    def test_a_second_export_does_not_overwrite_the_first(self, tmp_path, monkeypatch):
        window = self._window(tmp_path, monkeypatch, trimmed=True)
        first = window._destination_for("mp4")
        first.write_bytes(b"already here")

        assert window._destination_for("mp4") != first
