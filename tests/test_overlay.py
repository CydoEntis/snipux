from unittest.mock import Mock

import pytest
from PyQt6.QtCore import QPoint, QPointF, QRect, QRectF, QSizeF, Qt
from PyQt6.QtGui import QColor, QImage, QPainter, qRgb
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication

from snipux.capture import Frame
from snipux.overlay import Overlay, create_overlays

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
        confirmed.assert_called_once_with(QRectF(10, 10, 20, 20))

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
