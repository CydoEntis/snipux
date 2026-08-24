from unittest.mock import Mock

import pytest
from PyQt6.QtCore import QPoint, QPointF, QRect, QRectF, QSizeF, Qt
from PyQt6.QtGui import QColor, QImage, QPainter, QPainterPath, qRgb
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QWidget

from snipux.capture import Frame, X11WindowGeometryProvider
from snipux.design import color as design_color
from snipux.design import tokens
from snipux.overlay import (
    GeometryProvider,
    Handle,
    Overlay,
    OverlayWindow,
    SelectionMode,
    UnsupportedGeometryProvider,
    _HANDLE_CURSORS,
    create_overlays,
)

BASE_COLOR = qRgb(10, 20, 30)


@pytest.fixture(scope="module", autouse=True)
def qapp():
    # PyQt6 needs a live QApplication to construct any QWidget, even
    # offscreen. Module-scoped so every test in this file shares one.
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def make_frame(
    image_size=(200, 200), logical_size=(200, 200), logical_origin=(0, 0)
) -> Frame:
    image = QImage(*image_size, QImage.Format.Format_RGB32)
    image.fill(BASE_COLOR)
    return Frame(
        image=image,
        logical_origin=QPointF(*logical_origin),
        logical_size=QSizeF(*logical_size),
    )


def _blend(base: QColor, fg: QColor) -> QColor:
    """Plain-Python src-over compositing of `fg` (with its own alpha) atop
    an opaque `base`, to compute what the scrim should look like without
    depending on Qt's own compositor -- what TestOverlayWindow's scrim test
    checks its render against.
    """
    a = fg.alphaF()
    return QColor(
        round(base.red() * (1 - a) + fg.red() * a),
        round(base.green() * (1 - a) + fg.green() * a),
        round(base.blue() * (1 - a) + fg.blue() * a),
    )


class TestCreateOverlays:
    def test_create_overlays_returns_one_per_monitor_geometry(self):
        image = QImage(400, 200, QImage.Format.Format_RGB32)
        image.fill(QColor(0, 0, 255))
        painter = QPainter(image)
        painter.fillRect(QRect(0, 0, 200, 200), QColor(255, 0, 0))
        painter.end()
        frame = Frame(
            image=image, logical_origin=QPointF(0, 0), logical_size=QSizeF(400, 200)
        )
        left = QRectF(0, 0, 200, 200)
        right = QRectF(200, 0, 200, 200)

        overlays = create_overlays(frame, [left, right])

        assert len(overlays) == 2
        assert overlays[0]._monitor_frame.image.pixelColor(
            10, 10
        ) == frame.crop(left).image.pixelColor(10, 10)
        assert overlays[0]._monitor_frame.image.pixelColor(10, 10).red() == 255
        assert overlays[1]._monitor_frame.image.pixelColor(
            10, 10
        ) == frame.crop(right).image.pixelColor(10, 10)
        assert overlays[1]._monitor_frame.image.pixelColor(10, 10).blue() == 255


class TestVeil:
    def test_veil_dims_outside_selection_and_not_inside(self):
        frame = make_frame()
        overlay = Overlay(frame, QRectF(0, 0, 200, 200))
        overlay.set_selection(QRectF(50, 50, 50, 50))

        rendered = overlay.grab().toImage()
        base_color = QColor(10, 20, 30)

        assert rendered.pixelColor(70, 70) == base_color
        outside = rendered.pixelColor(10, 10)
        assert outside != base_color
        assert outside.red() < base_color.red()

    def test_no_selection_dims_the_whole_monitor(self):
        frame = make_frame()
        overlay = Overlay(frame, QRectF(0, 0, 200, 200))

        rendered = overlay.grab().toImage()
        base_color = QColor(10, 20, 30)

        for x, y in [(10, 10), (100, 100), (190, 190)]:
            assert rendered.pixelColor(x, y) != base_color

    def test_selection_spanning_two_monitors_dims_each_correctly(self):
        image = QImage(400, 200, QImage.Format.Format_RGB32)
        image.fill(BASE_COLOR)
        frame = Frame(
            image=image, logical_origin=QPointF(0, 0), logical_size=QSizeF(400, 200)
        )
        overlays = create_overlays(
            frame, [QRectF(0, 0, 200, 200), QRectF(200, 0, 200, 200)]
        )
        # Absolute selection straddling both monitors' geometries.
        selection = QRectF(150, 50, 100, 100)
        for overlay in overlays:
            overlay.set_selection(selection)

        base_color = QColor(10, 20, 30)

        left_image = overlays[0].grab().toImage()
        assert left_image.pixelColor(170, 70) == base_color  # inside, on the left
        assert left_image.pixelColor(10, 10) != base_color  # outside

        right_image = overlays[1].grab().toImage()
        assert right_image.pixelColor(20, 100) == base_color  # inside, on the right
        assert right_image.pixelColor(190, 190) != base_color  # outside


class TestSizeReadout:
    def test_size_label_shows_width_and_height_while_dragging(self):
        frame = make_frame(image_size=(300, 300), logical_size=(300, 300))
        overlay = Overlay(frame, QRectF(0, 0, 300, 300))

        overlay.set_selection(QRectF(10, 10, 40, 60))

        assert overlay._size_label.text() == "40 × 60"

    def test_size_readout_uses_logical_pixels_under_scaling(self):
        # Image is 2x the logical size (fractional/integer display scaling).
        frame = make_frame(image_size=(600, 600), logical_size=(300, 300))
        overlay = Overlay(frame, QRectF(0, 0, 300, 300))

        overlay.set_selection(QRectF(10, 10, 30, 20))

        assert overlay._size_label.text() == "30 × 20"


class TestMagnifier:
    def test_magnifier_draws_without_a_cursor_position(self):
        frame = make_frame()
        overlay = Overlay(frame, QRectF(0, 0, 200, 200))

        overlay.grab()  # must not raise with no mouse move simulated yet

    def test_magnifier_crosshair_centered_on_cursor(self):
        frame = make_frame()
        overlay = Overlay(frame, QRectF(0, 0, 200, 200))
        overlay._cursor_pos = QPointF(50, 50)

        rendered = overlay.grab().toImage()

        box_rect = QRectF(
            overlay._cursor_pos + Overlay.MAGNIFIER_OFFSET,
            QSizeF(Overlay.MAGNIFIER_BOX_SIZE, Overlay.MAGNIFIER_BOX_SIZE),
        )
        center = box_rect.center()
        sampled = rendered.pixelColor(round(center.x()), round(center.y()))

        assert sampled == Overlay.CROSSHAIR_COLOR

    def test_magnifier_samples_correct_region_under_scaling(self):
        # Image is 2x logical size, like the size-readout scaling test.
        image = QImage(400, 400, QImage.Format.Format_RGB32)
        image.fill(BASE_COLOR)
        marker_color = QColor(0, 255, 0)
        painter = QPainter(image)
        # Image-pixel (90,90)-(110,110): centered on image-pixel (100,100),
        # which is logical (50,50) at this 2x scale.
        painter.fillRect(QRect(90, 90, 20, 20), marker_color)
        painter.end()
        frame = Frame(
            image=image, logical_origin=QPointF(0, 0), logical_size=QSizeF(200, 200)
        )
        overlay = Overlay(frame, QRectF(0, 0, 200, 200))
        overlay._cursor_pos = QPointF(50, 50)  # logical position over the marker

        rendered = overlay.grab().toImage()

        box_rect = QRectF(
            overlay._cursor_pos + Overlay.MAGNIFIER_OFFSET,
            QSizeF(Overlay.MAGNIFIER_BOX_SIZE, Overlay.MAGNIFIER_BOX_SIZE),
        )
        # Offset from the exact center so this doesn't sample the
        # crosshair line itself (covered by the crosshair test above),
        # while staying inside the magnified marker region.
        sample_point = box_rect.center() + QPointF(15, 15)
        sampled = rendered.pixelColor(round(sample_point.x()), round(sample_point.y()))

        assert sampled == marker_color

    def test_magnifier_clamps_into_view_near_a_monitor_edge(self):
        # Unclamped, cursor + MAGNIFIER_OFFSET would place the box's left
        # edge past the widget's right edge, painting it fully off-window
        # and clipping it away entirely — nothing would show at all.
        frame = make_frame()
        overlay = Overlay(frame, QRectF(0, 0, 200, 200))
        overlay._cursor_pos = QPointF(195, 195)

        rendered = overlay.grab().toImage()

        box_x = min(
            overlay._cursor_pos.x() + Overlay.MAGNIFIER_OFFSET.x(),
            overlay.width() - Overlay.MAGNIFIER_BOX_SIZE,
        )
        box_y = min(
            overlay._cursor_pos.y() + Overlay.MAGNIFIER_OFFSET.y(),
            overlay.height() - Overlay.MAGNIFIER_BOX_SIZE,
        )
        box_rect = QRectF(
            QPointF(box_x, box_y),
            QSizeF(Overlay.MAGNIFIER_BOX_SIZE, Overlay.MAGNIFIER_BOX_SIZE),
        )
        center = box_rect.center()
        assert QRectF(overlay.rect()).contains(center)
        sampled = rendered.pixelColor(round(center.x()), round(center.y()))

        assert sampled == Overlay.CROSSHAIR_COLOR


class TestInteraction:
    def test_escape_emits_cancelled_and_enter_emits_confirmed(self):
        frame = make_frame()
        overlay = Overlay(frame, QRectF(0, 0, 200, 200))
        overlay.set_selection(QRectF(10, 10, 20, 20))
        cancelled = Mock()
        confirmed = Mock()
        overlay.cancelled.connect(cancelled)
        overlay.confirmed.connect(confirmed)

        QTest.keyClick(overlay, Qt.Key.Key_Escape)
        cancelled.assert_called_once()

        QTest.keyClick(overlay, Qt.Key.Key_Return)
        confirmed.assert_called_once_with(QRectF(10, 10, 20, 20), None)

    def test_right_click_emits_cancelled(self):
        frame = make_frame()
        overlay = Overlay(frame, QRectF(0, 0, 200, 200))
        cancelled = Mock()
        overlay.cancelled.connect(cancelled)

        QTest.mouseClick(overlay, Qt.MouseButton.RightButton, pos=QPoint(50, 50))

        cancelled.assert_called_once()

    def test_click_without_drag_is_a_misfire(self):
        frame = make_frame()
        overlay = Overlay(frame, QRectF(0, 0, 200, 200))
        confirmed = Mock()
        overlay.confirmed.connect(confirmed)

        QTest.mouseClick(overlay, Qt.MouseButton.LeftButton, pos=QPoint(50, 50))

        confirmed.assert_not_called()
        assert overlay._selection is None

    def test_drag_beyond_threshold_emits_confirmed(self):
        frame = make_frame()
        overlay = Overlay(frame, QRectF(0, 0, 200, 200))
        confirmed = Mock()
        overlay.confirmed.connect(confirmed)

        margin = QApplication.startDragDistance() + 10
        start = QPoint(20, 20)
        end = QPoint(20 + margin, 20 + margin)

        QTest.mousePress(overlay, Qt.MouseButton.LeftButton, pos=start)
        QTest.mouseMove(overlay, end)
        QTest.mouseRelease(overlay, Qt.MouseButton.LeftButton, pos=end)

        confirmed.assert_called_once()
        emitted_rect = confirmed.call_args[0][0]
        assert emitted_rect.width() == margin
        assert emitted_rect.height() == margin
        assert confirmed.call_args[0][1] is None


class TestUnsupportedGeometryProvider:
    def test_is_unavailable_and_reports_no_windows(self):
        provider = UnsupportedGeometryProvider()

        assert provider.is_available() is False
        assert provider.window_at(QPointF(10, 10)) is None


class TestFreeformMode:
    # Traces a proper L-shape (not a triangle) so the excluded-corner
    # assertion below is unambiguous: the notch at the top-right of the
    # bounding box is nowhere near the polygon's own edges.
    _STEM_TOP_LEFT = QPoint(20, 20)
    _STEM_TOP_RIGHT = QPoint(60, 20)
    _NOTCH_CORNER = QPoint(60, 80)
    _FOOT_TOP_RIGHT = QPoint(100, 80)
    _FOOT_BOTTOM_RIGHT = QPoint(100, 120)
    _FOOT_BOTTOM_LEFT = QPoint(20, 120)  # release point; close() returns to start

    def _trace_l_shape(self, overlay):
        QTest.mousePress(overlay, Qt.MouseButton.LeftButton, pos=self._STEM_TOP_LEFT)
        QTest.mouseMove(overlay, self._STEM_TOP_RIGHT)
        QTest.mouseMove(overlay, self._NOTCH_CORNER)
        QTest.mouseMove(overlay, self._FOOT_TOP_RIGHT)
        QTest.mouseMove(overlay, self._FOOT_BOTTOM_RIGHT)
        QTest.mouseRelease(
            overlay, Qt.MouseButton.LeftButton, pos=self._FOOT_BOTTOM_LEFT
        )

    def test_confirms_bounds_and_excludes_pixels_outside_the_path(self):
        frame = make_frame()
        overlay = Overlay(frame, QRectF(0, 0, 200, 200), mode=SelectionMode.FREEFORM)
        confirmed = Mock()
        overlay.confirmed.connect(confirmed)

        self._trace_l_shape(overlay)

        confirmed.assert_called_once()
        bounds, path = confirmed.call_args[0]
        assert bounds == QRectF(20, 20, 80, 100)
        assert isinstance(path, QPainterPath)
        assert path.contains(QPointF(30, 30))  # inside the stem
        assert path.contains(QPointF(80, 100))  # inside the foot
        # Inside the bounding box, but in the notch the L cuts away.
        assert not path.contains(QPointF(80, 40))

    def test_size_label_live_updates_mid_drag(self):
        frame = make_frame()
        overlay = Overlay(frame, QRectF(0, 0, 200, 200), mode=SelectionMode.FREEFORM)

        QTest.mousePress(overlay, Qt.MouseButton.LeftButton, pos=self._STEM_TOP_LEFT)
        QTest.mouseMove(overlay, self._STEM_TOP_RIGHT)
        QTest.mouseMove(overlay, self._NOTCH_CORNER)

        # Bounding box of the path so far: (20,20) to (60,80).
        assert overlay._size_label.text() == "40 × 60"

        QTest.mouseRelease(overlay, Qt.MouseButton.LeftButton, pos=self._NOTCH_CORNER)

    def test_press_release_with_no_movement_is_a_misfire(self):
        frame = make_frame()
        overlay = Overlay(frame, QRectF(0, 0, 200, 200), mode=SelectionMode.FREEFORM)
        confirmed = Mock()
        overlay.confirmed.connect(confirmed)

        QTest.mouseClick(overlay, Qt.MouseButton.LeftButton, pos=QPoint(50, 50))

        confirmed.assert_not_called()
        assert overlay._selection is None

    def test_closed_loop_back_near_start_still_confirms(self):
        # Anchor-to-release distance would be tiny here; only the traced
        # path's own bounding-rect diagonal should decide misfire or not.
        frame = make_frame()
        overlay = Overlay(frame, QRectF(0, 0, 200, 200), mode=SelectionMode.FREEFORM)
        confirmed = Mock()
        overlay.confirmed.connect(confirmed)

        anchor = QPoint(20, 20)
        QTest.mousePress(overlay, Qt.MouseButton.LeftButton, pos=anchor)
        QTest.mouseMove(overlay, QPoint(20, 120))
        QTest.mouseMove(overlay, QPoint(100, 120))
        QTest.mouseMove(overlay, QPoint(100, 20))
        near_start = QPoint(22, 22)
        QTest.mouseMove(overlay, near_start)
        QTest.mouseRelease(overlay, Qt.MouseButton.LeftButton, pos=near_start)

        confirmed.assert_called_once()


class _FakeWindowProvider(GeometryProvider):
    """Reports one fixed window rect for points inside it, None elsewhere."""

    def __init__(self, rect: QRectF):
        self._rect = rect

    def is_available(self) -> bool:
        return True

    def window_at(self, point: QPointF) -> QRectF | None:
        return self._rect if self._rect.contains(point) else None


class TestWindowMode:
    WINDOW_RECT = QRectF(30, 30, 50, 50)  # covers points (30,30)-(80,80)
    HIT_POINT = QPoint(50, 50)
    MISS_POINT = QPoint(10, 10)

    def test_click_on_a_window_confirms_it_immediately(self):
        frame = make_frame()
        overlay = Overlay(
            frame,
            QRectF(0, 0, 200, 200),
            mode=SelectionMode.WINDOW,
            geometry_provider=_FakeWindowProvider(self.WINDOW_RECT),
        )
        confirmed = Mock()
        overlay.confirmed.connect(confirmed)

        QTest.mouseClick(overlay, Qt.MouseButton.LeftButton, pos=self.HIT_POINT)

        confirmed.assert_called_once_with(self.WINDOW_RECT, None)

    def test_hover_previews_and_clears_on_miss(self):
        frame = make_frame()
        overlay = Overlay(
            frame,
            QRectF(0, 0, 200, 200),
            mode=SelectionMode.WINDOW,
            geometry_provider=_FakeWindowProvider(self.WINDOW_RECT),
        )
        # A hover-only move (no button held) is only delivered to a widget
        # that has actually been shown and exposed; unlike a drag, there is
        # no preceding press to establish that the widget is receiving
        # mouse events.
        overlay.show()
        QTest.qWaitForWindowExposed(overlay)

        QTest.mouseMove(overlay, self.HIT_POINT)
        assert overlay._selection == self.WINDOW_RECT

        QTest.mouseMove(overlay, self.MISS_POINT)
        assert overlay._selection is None

    def test_drag_from_a_miss_falls_back_to_rectangle(self):
        frame = make_frame()
        overlay = Overlay(
            frame,
            QRectF(0, 0, 200, 200),
            mode=SelectionMode.WINDOW,
            geometry_provider=_FakeWindowProvider(self.WINDOW_RECT),
        )
        confirmed = Mock()
        overlay.confirmed.connect(confirmed)

        end = QPoint(150, 150)
        QTest.mousePress(overlay, Qt.MouseButton.LeftButton, pos=self.MISS_POINT)
        QTest.mouseMove(overlay, end)
        QTest.mouseRelease(overlay, Qt.MouseButton.LeftButton, pos=end)

        confirmed.assert_called_once_with(
            QRectF(QPointF(self.MISS_POINT), QPointF(end)).normalized(), None
        )

    def test_press_on_hit_then_drag_away_still_confirms_the_window(self):
        frame = make_frame()
        overlay = Overlay(
            frame,
            QRectF(0, 0, 200, 200),
            mode=SelectionMode.WINDOW,
            geometry_provider=_FakeWindowProvider(self.WINDOW_RECT),
        )
        confirmed = Mock()
        overlay.confirmed.connect(confirmed)

        drift = QPoint(150, 150)
        QTest.mousePress(overlay, Qt.MouseButton.LeftButton, pos=self.HIT_POINT)
        QTest.mouseMove(overlay, drift)
        QTest.mouseRelease(overlay, Qt.MouseButton.LeftButton, pos=drift)

        confirmed.assert_called_once_with(self.WINDOW_RECT, None)

    def test_without_a_provider_behaves_like_rectangle_mode(self):
        frame = make_frame()
        overlay = Overlay(frame, QRectF(0, 0, 200, 200), mode=SelectionMode.WINDOW)
        confirmed = Mock()
        overlay.confirmed.connect(confirmed)

        margin = QApplication.startDragDistance() + 10
        start = QPoint(20, 20)
        end = QPoint(20 + margin, 20 + margin)
        QTest.mousePress(overlay, Qt.MouseButton.LeftButton, pos=start)
        QTest.mouseMove(overlay, end)
        QTest.mouseRelease(overlay, Qt.MouseButton.LeftButton, pos=end)

        confirmed.assert_called_once_with(
            QRectF(QPointF(start), QPointF(end)).normalized(), None
        )


class TestX11WindowGeometryProviderIntegration:
    """Proves the real X11 provider (not just the fake in TestWindowMode
    above) satisfies Overlay's expectations end to end: a real `wmctrl -lG`
    call, mocked, feeding straight into window-mode click handling.
    """

    WMCTRL_STDOUT = "0x1  0 30 30 50 50 host1 Some Window\n"
    HIT_POINT = QPoint(50, 50)  # inside (30,30)-(80,80)

    def test_click_on_a_listed_window_confirms_its_geometry(self, monkeypatch):
        monkeypatch.setattr(
            "snipux.capture.shutil.which", lambda binary: "/usr/bin/wmctrl"
        )
        monkeypatch.setattr(
            "snipux.capture.subprocess.run",
            lambda *a, **k: Mock(stdout=self.WMCTRL_STDOUT, returncode=0),
        )
        provider = X11WindowGeometryProvider()

        frame = make_frame()
        overlay = Overlay(
            frame,
            QRectF(0, 0, 200, 200),
            mode=SelectionMode.WINDOW,
            geometry_provider=provider,
        )
        confirmed = Mock()
        overlay.confirmed.connect(confirmed)

        QTest.mouseClick(overlay, Qt.MouseButton.LeftButton, pos=self.HIT_POINT)

        confirmed.assert_called_once_with(QRectF(30, 30, 50, 50), None)


class TestFullScreenMode:
    def test_selection_is_the_full_geometry_before_any_mouse_event(self):
        frame = make_frame()
        geometry = QRectF(0, 0, 200, 200)

        overlay = Overlay(frame, geometry, mode=SelectionMode.FULL_SCREEN)

        assert overlay._selection == geometry

    def test_bare_click_confirms_with_no_drag(self):
        frame = make_frame()
        overlay = Overlay(frame, QRectF(0, 0, 200, 200), mode=SelectionMode.FULL_SCREEN)
        confirmed = Mock()
        overlay.confirmed.connect(confirmed)

        QTest.mouseClick(overlay, Qt.MouseButton.LeftButton, pos=QPoint(50, 50))

        confirmed.assert_called_once_with(QRectF(0, 0, 200, 200), None)

    def test_selection_does_not_shrink_while_dragging(self):
        frame = make_frame()
        geometry = QRectF(0, 0, 200, 200)
        overlay = Overlay(frame, geometry, mode=SelectionMode.FULL_SCREEN)

        QTest.mousePress(overlay, Qt.MouseButton.LeftButton, pos=QPoint(20, 20))
        QTest.mouseMove(overlay, QPoint(100, 100))

        assert overlay._selection == geometry
        assert overlay._size_label.text() == "200 × 200"

        QTest.mouseRelease(overlay, Qt.MouseButton.LeftButton, pos=QPoint(100, 100))

    def test_create_overlays_selects_the_union_of_all_monitors(self):
        image = QImage(400, 200, QImage.Format.Format_RGB32)
        image.fill(BASE_COLOR)
        frame = Frame(
            image=image, logical_origin=QPointF(0, 0), logical_size=QSizeF(400, 200)
        )
        left = QRectF(0, 0, 200, 200)
        right = QRectF(200, 0, 200, 200)

        overlays = create_overlays(
            frame, [left, right], mode=SelectionMode.FULL_SCREEN
        )

        union = QRectF(0, 0, 400, 200)
        assert overlays[0]._selection == union
        assert overlays[1]._selection == union

        base_color = QColor(10, 20, 30)
        for overlay in overlays:
            rendered = overlay.grab().toImage()
            # Avoids the top-left corner: the size-readout label paints its
            # own (semi-transparent black) background there, which is
            # unrelated to what this test is checking — that the veil
            # itself has no dimmed hole anywhere.
            for x, y in [(10, 190), (100, 100), (190, 190)]:
                assert rendered.pixelColor(x, y) == base_color


class TestOverlayWindow:
    """The redesign's shell (SNX-31): a single window over the whole
    virtual desktop, not one per monitor -- see OverlayWindow's docstring
    for how this differs from `Overlay` above.
    """

    def test_frameless_always_on_top_and_covers_the_virtual_desktop(self):
        frame = make_frame(
            image_size=(300, 200), logical_size=(300, 200), logical_origin=(50, 20)
        )

        overlay = OverlayWindow(frame)

        flags = overlay.windowFlags()
        assert flags & Qt.WindowType.FramelessWindowHint
        assert flags & Qt.WindowType.WindowStaysOnTopHint
        assert overlay.geometry() == QRect(50, 20, 300, 200)

    def test_paints_the_captured_frame_as_the_background(self):
        frame = make_frame()
        overlay = OverlayWindow(frame)
        overlay.set_selection(QRect(50, 50, 50, 50))

        rendered = overlay.grab().toImage()

        assert rendered.pixelColor(70, 70) == QColor(10, 20, 30)

    def test_selection_is_undimmed_at_1_to_1(self):
        # image_size == logical_size (no scaling), so the undimmed hole
        # should show exactly the base colour, pixel for pixel. Sampled
        # away from the corners/edges (since SNX-32's frame chrome now
        # legitimately paints over those, per the "frame stroke, handles"
        # layer in the spec's layer list) -- this test is about the
        # interior, not the frame, which TestCornerBrackets/TestEdgeHandles
        # cover.
        frame = make_frame(image_size=(200, 200), logical_size=(200, 200))
        overlay = OverlayWindow(frame)
        overlay.set_selection(QRect(50, 50, 50, 50))

        rendered = overlay.grab().toImage()

        for x, y in [(60, 60), (75, 75), (90, 90)]:
            assert rendered.pixelColor(x, y) == QColor(10, 20, 30)

    def test_scrim_outside_selection_uses_the_dim_token_colour_and_alpha(self):
        frame = make_frame()
        overlay = OverlayWindow(frame)
        overlay.set_selection(QRect(50, 50, 50, 50))

        rendered = overlay.grab().toImage()
        expected = _blend(QColor(10, 20, 30), design_color("DIM"))
        sampled = rendered.pixelColor(10, 10)

        # Small tolerance for Qt's own (premultiplied-alpha) rounding vs.
        # the plain-float blend computed in _blend above.
        assert sampled.red() == pytest.approx(expected.red(), abs=2)
        assert sampled.green() == pytest.approx(expected.green(), abs=2)
        assert sampled.blue() == pytest.approx(expected.blue(), abs=2)

    def test_no_selection_dims_the_whole_window(self):
        frame = make_frame()
        overlay = OverlayWindow(frame)

        rendered = overlay.grab().toImage()
        base_color = QColor(10, 20, 30)

        for x, y in [(10, 10), (100, 100), (190, 190)]:
            assert rendered.pixelColor(x, y) != base_color

    def test_selection_is_held_in_window_not_absolute_coordinates(self):
        # logical_origin != (0, 0) simulates a monitor away from the
        # virtual desktop's own top-left. If the selection were (mis)read
        # as an absolute virtual-desktop rect -- the way `Overlay` above
        # uses it -- this window-local selection would land nowhere near
        # (0, 0) inside this widget and the corner would stay dimmed.
        frame = make_frame(
            image_size=(200, 200), logical_size=(200, 200), logical_origin=(500, 300)
        )
        overlay = OverlayWindow(frame)
        overlay.set_selection(QRect(0, 0, 50, 50))

        rendered = overlay.grab().toImage()

        assert rendered.pixelColor(10, 10) == QColor(10, 20, 30)

    def test_scrim_is_painted_by_the_widget_itself_not_a_child_widget(self):
        # Per the spec: a translucent child stacked over the whole window
        # would sit above the (future) ink layer and eat its mouse events,
        # so nothing here should be a child widget covering the window.
        frame = make_frame()
        overlay = OverlayWindow(frame)

        assert overlay.findChildren(QWidget) == []


class TestSelectionStroke:
    """SNX-32: the two coincident 1px strokes -- solid white under an
    animated dashed dark one -- that make the marching ants.
    """

    def test_uses_the_dash_pattern_and_colours_from_tokens(self):
        frame = make_frame()
        overlay = OverlayWindow(frame)
        overlay.set_selection(QRect(10, 10, 50, 50))

        solid, dashed = overlay._stroke_pens()

        assert solid.color() == design_color("SEL_STROKE")
        assert solid.widthF() == tokens.Metric.SEL_STROKE_W
        assert dashed.color() == design_color("SEL_ANTS")
        assert dashed.dashPattern() == list(tokens.Metric.ANTS_DASH)
        assert dashed.dashOffset() == 0.0

    def test_renders_both_the_solid_and_dashed_layers(self):
        # Sampled in the gap between the corner bracket and the edge handle
        # (computed from their own geometry, not a hardcoded pixel), so this
        # is reading pure stroke -- not the opaque white chrome painted over
        # it elsewhere on the same edge.
        frame = make_frame(image_size=(300, 300), logical_size=(300, 300))
        overlay = OverlayWindow(frame)
        overlay.set_selection(QRect(50, 50, 100, 80))

        bracket_right = round(overlay._bracket_path(Handle.TOP_LEFT).boundingRect().right())
        handle_left = round(overlay._edge_handle_rect(Handle.TOP).left())
        assert bracket_right < handle_left  # otherwise there's no gap to sample

        rendered = overlay.grab().toImage()
        row = [
            rendered.pixelColor(x, 50).getRgb()[:3]
            for x in range(bracket_right, handle_left)
        ]

        white = design_color("SEL_STROKE")
        dark = design_color("SEL_ANTS")
        # Not exact-equality: the white layer blends with the frozen frame
        # underneath at 92% alpha (SEL_STROKE_ALPHA), so only the dark,
        # fully-opaque dash colour survives compositing unchanged.
        assert any(c[0] > white.red() - 40 for c in row), row  # a light/white sample
        assert any(abs(c[0] - dark.red()) < 5 for c in row), row  # a dark sample

    def test_ants_offset_advances_each_tick_and_wraps_at_the_dash_cycle(self):
        frame = make_frame()
        overlay = OverlayWindow(frame)
        overlay.set_selection(QRect(10, 10, 50, 50))
        cycle = sum(tokens.Metric.ANTS_DASH)

        offsets = [overlay._dash_offset]
        for _ in range(cycle * 3):
            overlay._advance_ants()
            offsets.append(overlay._dash_offset)

        assert offsets[1] != offsets[0]
        assert all(0 <= o < cycle for o in offsets)
        # The pen actually used for painting picks up the new offset too --
        # advancing state that nothing reads would be a silent no-op.
        assert overlay._stroke_pens()[1].dashOffset() == overlay._dash_offset

    def test_ants_timer_runs_only_while_the_overlay_is_visible(self):
        frame = make_frame()
        overlay = OverlayWindow(frame)
        overlay.set_selection(QRect(10, 10, 50, 50))

        assert not overlay._ants_timer.isActive()

        overlay.show()
        QTest.qWaitForWindowExposed(overlay)
        assert overlay._ants_timer.isActive()

        overlay.hide()
        assert not overlay._ants_timer.isActive()


class TestCornerBrackets:
    """SNX-32: the L-shaped corner brackets, which double as the corner
    handles' only visible chrome.
    """

    SEL = QRect(50, 50, 100, 80)

    def _overlay(self):
        frame = make_frame(image_size=(300, 300), logical_size=(300, 300))
        overlay = OverlayWindow(frame)
        overlay.set_selection(self.SEL)
        return overlay

    @pytest.mark.parametrize(
        "handle",
        [Handle.TOP_LEFT, Handle.TOP_RIGHT, Handle.BOTTOM_LEFT, Handle.BOTTOM_RIGHT],
    )
    def test_bracket_bounds_are_the_arm_length_straddling_the_corner(self, handle):
        overlay = self._overlay()
        sel = QRectF(self.SEL)
        offset = overlay._CORNER_BRACKET_OFFSET
        length = tokens.Metric.CORNER_LEN

        bounds = overlay._bracket_path(handle).boundingRect()

        assert bounds.width() == pytest.approx(length)
        assert bounds.height() == pytest.approx(length)
        expected_left = sel.left() - offset if "left" in handle.value else sel.right() + offset - length
        expected_top = sel.top() - offset if "top" in handle.value else sel.bottom() + offset - length
        assert bounds.left() == pytest.approx(expected_left)
        assert bounds.top() == pytest.approx(expected_top)

    def test_bracket_arms_are_painted_at_the_token_thickness(self):
        # Along the top-left bracket's horizontal arm, a row inside the
        # bracket's box should be solid white for exactly CORNER_W pixels
        # before falling back to the (dimmed) background.
        overlay = self._overlay()
        rendered = overlay.grab().toImage()

        box_left = round(overlay._bracket_path(Handle.TOP_LEFT).boundingRect().left())
        white_rows = 0
        for y in range(box_left, box_left + tokens.Metric.CORNER_LEN):
            # Sample a column comfortably inside the arm's length, away from
            # the rounded tip and the inner elbow.
            if rendered.pixelColor(self.SEL.left() + 15, y) == QColor(255, 255, 255):
                white_rows += 1
        assert white_rows == tokens.Metric.CORNER_W


class TestEdgeHandles:
    """SNX-32: the rounded bar handle centred on each edge."""

    SEL = QRect(50, 50, 100, 80)

    def _overlay(self):
        frame = make_frame(image_size=(300, 300), logical_size=(300, 300))
        overlay = OverlayWindow(frame)
        overlay.set_selection(self.SEL)
        return overlay

    def test_dimensions_match_tokens_and_are_centred_on_the_edge(self):
        overlay = self._overlay()
        sel = QRectF(self.SEL)
        long_, short = tokens.Metric.HANDLE_LONG, tokens.Metric.HANDLE_SHORT

        top = overlay._edge_handle_rect(Handle.TOP)
        assert (top.width(), top.height()) == (long_, short)
        assert top.center().x() == pytest.approx(sel.center().x())

        left = overlay._edge_handle_rect(Handle.LEFT)
        assert (left.width(), left.height()) == (short, long_)
        assert left.center().y() == pytest.approx(sel.center().y())

    def test_handle_is_painted_white_at_its_centre(self):
        overlay = self._overlay()
        rendered = overlay.grab().toImage()

        for handle in (Handle.TOP, Handle.BOTTOM, Handle.LEFT, Handle.RIGHT):
            center = overlay._edge_handle_rect(handle).center()
            sampled = rendered.pixelColor(round(center.x()), round(center.y()))
            assert sampled == QColor(255, 255, 255), handle


class TestHandleCursors:
    """SNX-32: hovering a handle previews the direction it resizes in."""

    SEL = QRect(50, 50, 100, 80)

    def _shown_overlay(self):
        frame = make_frame(image_size=(300, 300), logical_size=(300, 300))
        overlay = OverlayWindow(frame)
        overlay.set_selection(self.SEL)
        overlay.show()
        QTest.qWaitForWindowExposed(overlay)
        return overlay

    @pytest.mark.parametrize(
        "point, handle",
        [
            (QPoint(50, 50), Handle.TOP_LEFT),
            (QPoint(150, 50), Handle.TOP_RIGHT),
            (QPoint(50, 130), Handle.BOTTOM_LEFT),
            (QPoint(150, 130), Handle.BOTTOM_RIGHT),
            (QPoint(100, 50), Handle.TOP),
            (QPoint(100, 130), Handle.BOTTOM),
            (QPoint(50, 90), Handle.LEFT),
            (QPoint(150, 90), Handle.RIGHT),
        ],
    )
    def test_cursor_matches_the_handles_resize_direction(self, point, handle):
        overlay = self._shown_overlay()

        QTest.mouseMove(overlay, point)

        assert overlay.cursor().shape() == _HANDLE_CURSORS[handle]

    def test_cursor_resets_away_from_any_handle(self):
        overlay = self._shown_overlay()

        QTest.mouseMove(overlay, QPoint(50, 50))
        assert overlay.cursor().shape() == Qt.CursorShape.SizeFDiagCursor

        QTest.mouseMove(overlay, QPoint(100, 90))  # deep inside the selection
        assert overlay.cursor().shape() == Qt.CursorShape.ArrowCursor


class TestReframing:
    """SNX-33: dragging a handle re-frames the live selection, per
    docs/design/overlay-redesign.md's "Re-framing" section -- with the
    ticket's one deliberate deviation from that spec: the minimum size is
    16x16 (`tokens.Metric.SEL_MIN_W/H`), not the spec's 200x140.
    """

    def _overlay(self, size=(400, 400)):
        frame = make_frame(image_size=size, logical_size=size)
        overlay = OverlayWindow(frame)
        return overlay

    def _drag(self, overlay, press_pos, move_pos):
        QTest.mousePress(overlay, Qt.MouseButton.LeftButton, pos=press_pos)
        QTest.mouseMove(overlay, move_pos)
        QTest.mouseRelease(overlay, Qt.MouseButton.LeftButton, pos=move_pos)

    def test_dragging_an_edge_handle_moves_only_that_edge(self):
        overlay = self._overlay()
        overlay.set_selection(QRect(100, 100, 150, 100))
        press = overlay._edge_handle_rect(Handle.TOP).center().toPoint()

        self._drag(overlay, press, QPoint(175, 60))

        sel = overlay._selection
        # Top moved to the drag target; every other edge -- in particular
        # the opposite (bottom) edge -- is exactly where it started.
        assert (sel.x(), sel.y()) == (100, 60)
        assert (sel.width(), sel.height()) == (150, 140)

    def test_dragging_a_corner_moves_both_its_edges_opposite_corner_fixed(self):
        # A generous window, so the floating-bar clamp (covered on its own
        # below) doesn't also kick in here and muddy what this test is
        # checking.
        overlay = self._overlay(size=(500, 500))
        overlay.set_selection(QRect(100, 100, 150, 100))
        press = overlay._corner_hit_rect(Handle.BOTTOM_RIGHT).center().toPoint()

        self._drag(overlay, press, QPoint(320, 280))

        sel = overlay._selection
        # Top-left corner (the anchor for a bottom-right drag) is untouched.
        assert (sel.x(), sel.y()) == (100, 100)
        assert (sel.width(), sel.height()) == (220, 180)

    def test_drag_cannot_shrink_below_the_minimum_size_in_tokens(self):
        overlay = self._overlay()
        overlay.set_selection(QRect(100, 100, 150, 100))
        press = overlay._edge_handle_rect(Handle.RIGHT).center().toPoint()

        # Dragged well past the left (anchor) edge, which would invert the
        # rect if nothing stopped it.
        self._drag(overlay, press, QPoint(50, 150))

        sel = overlay._selection
        assert sel.x() == 100  # anchor edge never moved
        assert sel.width() == tokens.Metric.SEL_MIN_W == 16

    def test_drag_keeps_the_selection_clear_of_the_left_and_top_edges(self):
        overlay = self._overlay()
        overlay.set_selection(QRect(100, 100, 150, 100))
        press = overlay._corner_hit_rect(Handle.TOP_LEFT).center().toPoint()

        self._drag(overlay, press, QPoint(-50, -50))

        sel = overlay._selection
        assert sel.x() == 0  # x >= 0
        assert sel.y() == 52  # y >= 52, clear of the hint HUD

    def test_drag_keeps_the_selection_inside_the_window(self):
        overlay = self._overlay(size=(400, 400))
        overlay.set_selection(QRect(100, 100, 150, 100))
        press = overlay._corner_hit_rect(Handle.BOTTOM_RIGHT).center().toPoint()

        self._drag(overlay, press, QPoint(900, 900))

        sel = overlay._selection
        assert sel.x() + sel.width() <= overlay.width()
        assert sel.y() + sel.height() <= overlay.height()
        # The floating-bar clamp is the tighter of the two bottom bounds
        # here, so it's the one actually reached.
        assert sel.y() + sel.height() == overlay.height() - overlay._BAR_ROOM

    def test_bar_room_clamp_gives_way_to_the_minimum_rather_than_the_reverse(self):
        # A selection already pinned near the bottom of a short window: the
        # floating-bar clamp alone would force the selection below the
        # minimum height. Per the ticket, the minimum wins -- the bar-room
        # clamp is skipped rather than shrinking the selection further.
        overlay = self._overlay(size=(200, 200))
        overlay.set_selection(QRect(20, 170, 100, 16))
        bar_limit = overlay.height() - overlay._BAR_ROOM
        assert bar_limit < 170 + tokens.Metric.SEL_MIN_H  # the clamp would conflict
        press = overlay._edge_handle_rect(Handle.BOTTOM).center().toPoint()

        self._drag(overlay, press, QPoint(70, 195))

        sel = overlay._selection
        assert sel.y() == 170  # anchor (top) never moved
        assert sel.height() == 25  # 195 - 170, not clamped down to bar_limit
        assert sel.y() + sel.height() > bar_limit


class TestHandlePressDoesNotStartAStroke:
    """SNX-33: "Handle presses must not start a stroke -- stop event
    propagation at the handle." OverlayWindow has no drawing/ink of its own
    yet (a later ticket), so this asserts the boundary the spec calls for:
    a handle hit is consumed as a resize and nothing else, while a miss
    leaves no resize state behind for a future stroke-start to trip over.
    """

    def _overlay(self):
        frame = make_frame(image_size=(300, 300), logical_size=(300, 300))
        overlay = OverlayWindow(frame)
        overlay.set_selection(QRect(50, 50, 100, 80))
        return overlay

    def test_press_on_a_handle_starts_a_resize_not_a_stroke(self):
        overlay = self._overlay()
        press = overlay._corner_hit_rect(Handle.TOP_LEFT).center().toPoint()

        QTest.mousePress(overlay, Qt.MouseButton.LeftButton, pos=press)

        assert overlay._active_handle is Handle.TOP_LEFT

        QTest.mouseRelease(overlay, Qt.MouseButton.LeftButton, pos=press)
        assert overlay._active_handle is None

    def test_press_away_from_every_handle_starts_no_resize(self):
        overlay = self._overlay()
        original = QRect(overlay._selection)

        QTest.mousePress(
            overlay, Qt.MouseButton.LeftButton, pos=QPoint(100, 90)
        )  # deep inside the selection, nowhere near a handle

        assert overlay._active_handle is None
        assert overlay._selection == original
