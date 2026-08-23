from unittest.mock import Mock

import pytest
from PyQt6.QtCore import QPoint, QPointF, QRect, QRectF, QSizeF, Qt
from PyQt6.QtGui import QColor, QImage, QPainter, QPainterPath, qRgb
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication

from snipux.capture import Frame, X11WindowGeometryProvider
from snipux.overlay import (
    GeometryProvider,
    Overlay,
    SelectionMode,
    UnsupportedGeometryProvider,
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
