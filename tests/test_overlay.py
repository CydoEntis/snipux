from unittest.mock import Mock

import pytest
from PyQt6.QtCore import QPoint, QPointF, QRect, QRectF, QSize, QSizeF, Qt
from PyQt6.QtGui import QColor, QIcon, QImage, QPainter, QPainterPath, qRgb
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QColorDialog, QPushButton, QSlider, QWidget

import snipux.app as app_module
from snipux.capture import Frame, X11WindowGeometryProvider
from snipux.design import color as design_color
from snipux.design import tokens
from snipux.shapes import Rectangle
from snipux.overlay import (
    BlurTray,
    FloatingBar,
    GeometryProvider,
    Handle,
    Overlay,
    OverlayWindow,
    SelectionMode,
    SettingsTray,
    UnsupportedGeometryProvider,
    _BlurModeWell,
    _CustomColorButton,
    _Divider,
    _HANDLE_CURSORS,
    _PreviewDot,
    _SegmentButton,
    _SwatchButton,
    _TOOL_SHORTCUT_KEYS,
    _ToolPill,
    _tool_label,
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
        # Per the spec: a translucent child stacked over the *whole window*
        # would sit above the ink layer and eat its mouse events, so no
        # child widget's geometry may cover the full window rect -- unlike
        # SNX-31, when this assertion was last "no children at all," SNX-40
        # gives this window a legitimate child (the floating bar), which is
        # exactly why the check now has to be about coverage, not count.
        frame = make_frame()
        overlay = OverlayWindow(frame)
        window_rect = QRect(overlay.rect())

        for child in overlay.findChildren(QWidget):
            assert not child.geometry().contains(window_rect), child


class TestOverlayWindowMarks:
    """SNX-34: marks live in this window's own coordinates and are clipped
    to the selection at paint time -- never made selection-relative, and
    never deleted just because a re-frame currently hides them.
    """

    RED = QColor(255, 0, 0)

    def _mark(self, start, end, colour=None):
        return Rectangle(
            colour=colour or self.RED, stroke_width=6, start=QPointF(*start), end=QPointF(*end)
        )

    def test_add_mark_stores_it_unmodified_in_window_coordinates(self):
        frame = make_frame(image_size=(200, 200), logical_size=(200, 200))
        overlay = OverlayWindow(frame)
        mark = self._mark((30, 30), (50, 50))

        overlay.add_mark(mark)

        # Same object, same points -- add_mark never rewrites them relative
        # to the selection, which is the whole point of this coordinate
        # convention per the class/module docstrings.
        assert overlay.marks == (mark,)
        assert overlay.marks[0].start == QPointF(30, 30)

    def test_marks_is_a_snapshot_not_a_view_of_the_live_list(self):
        frame = make_frame(image_size=(200, 200), logical_size=(200, 200))
        overlay = OverlayWindow(frame)
        overlay.add_mark(self._mark((0, 0), (10, 10)))

        snapshot = overlay.marks
        overlay.add_mark(self._mark((20, 20), (30, 30)))

        assert len(snapshot) == 1  # unaffected by the add_mark() call after it
        assert len(overlay.marks) == 2

    def test_paints_a_mark_inside_the_selection(self):
        frame = make_frame(image_size=(200, 200), logical_size=(200, 200))
        overlay = OverlayWindow(frame)
        overlay.set_selection(QRect(0, 0, 200, 200))
        overlay.add_mark(self._mark((20, 20), (80, 80)))

        rendered = overlay.grab().toImage()

        assert rendered.pixelColor(20, 50) == self.RED  # left border

    def test_mark_outside_the_selection_is_clipped_not_painted(self):
        frame = make_frame(image_size=(200, 200), logical_size=(200, 200))
        overlay = OverlayWindow(frame)
        overlay.set_selection(QRect(100, 100, 50, 50))
        overlay.add_mark(self._mark((10, 10), (30, 30)))

        rendered = overlay.grab().toImage()

        assert rendered.pixelColor(20, 20) != self.RED

    def test_mark_reappears_once_the_selection_grows_back_over_it(self):
        # The mark was never deleted by the clip above -- it was only
        # hidden -- so widening the selection back over it must show it
        # again with no further calls into the ink layer.
        frame = make_frame(image_size=(200, 200), logical_size=(200, 200))
        overlay = OverlayWindow(frame)
        overlay.set_selection(QRect(100, 100, 50, 50))
        overlay.add_mark(self._mark((10, 10), (30, 30)))
        overlay.grab()  # one paint pass while hidden by the narrow selection

        overlay.set_selection(QRect(0, 0, 200, 200))
        rendered = overlay.grab().toImage()

        assert rendered.pixelColor(10, 20) == self.RED  # left border

    def test_reframing_leaves_a_mark_over_the_same_content(self):
        # A mark drawn inside the selection must stay over the same pixels
        # after the selection is re-framed -- the whole reason ink moved out
        # of selection-relative coordinates. Growing the selection (same
        # top-left) must not shift where the mark's own left border paints.
        frame = make_frame(image_size=(200, 200), logical_size=(200, 200))
        overlay = OverlayWindow(frame)
        overlay.set_selection(QRect(0, 0, 100, 100))
        overlay.add_mark(self._mark((20, 20), (40, 40)))

        before = overlay.grab().toImage().pixelColor(20, 30)

        overlay.set_selection(QRect(0, 0, 150, 150))
        after = overlay.grab().toImage().pixelColor(20, 30)

        assert before == self.RED
        assert after == self.RED

    def test_rendered_image_positions_marks_by_the_selection_origin(self):
        frame = make_frame(image_size=(200, 200), logical_size=(200, 200))
        overlay = OverlayWindow(frame)
        overlay.set_selection(QRect(50, 50, 100, 100))
        overlay.add_mark(self._mark((60, 60), (90, 90)))

        result = overlay.rendered_image()

        assert result.width() == 100
        assert result.height() == 100
        # (60, 60) in window coordinates is (10, 10) inside the crop.
        assert result.pixelColor(10, 20) == self.RED


class TestEraserTool:
    """SNX-38: per-shape hit-testing itself lives on `Shape` (shapes.py --
    see TestShapeHitTest in test_shapes.py); this class covers how
    OverlayWindow wires that into a click, and the eraser's own
    single-slot undo.
    """

    RED = QColor(255, 0, 0)
    BLUE = QColor(0, 0, 255)

    def _mark(self, start, end, colour=None):
        return Rectangle(
            colour=colour or self.RED, stroke_width=6, start=QPointF(*start), end=QPointF(*end)
        )

    def _overlay(self, selection=QRect(0, 0, 200, 200)):
        frame = make_frame(image_size=(200, 200), logical_size=(200, 200))
        overlay = OverlayWindow(frame)
        overlay.set_selection(selection)
        return overlay

    def test_click_with_eraser_active_removes_the_topmost_hit_mark(self):
        overlay = self._overlay()
        bottom = self._mark((20, 20), (80, 80), colour=self.RED)
        top = self._mark((20, 20), (80, 80), colour=self.BLUE)  # coincides with `bottom`
        overlay.add_mark(bottom)
        overlay.add_mark(top)
        overlay.set_eraser_active(True)

        QTest.mousePress(overlay, Qt.MouseButton.LeftButton, pos=QPoint(20, 50))

        # Both marks sit under the click; only the last-drawn (topmost) one
        # is gone -- draw order, per the ticket, not add order coincidence.
        assert overlay.marks == (bottom,)

    def test_click_with_eraser_inactive_removes_nothing(self):
        overlay = self._overlay()
        mark = self._mark((20, 20), (80, 80))
        overlay.add_mark(mark)
        # set_eraser_active is never called: default state is inactive.

        QTest.mousePress(overlay, Qt.MouseButton.LeftButton, pos=QPoint(20, 50))

        assert overlay.marks == (mark,)

    def test_click_on_empty_space_with_eraser_active_removes_nothing_and_does_not_raise(self):
        overlay = self._overlay()
        mark = self._mark((20, 20), (80, 80))
        overlay.add_mark(mark)
        overlay.set_eraser_active(True)

        QTest.mousePress(overlay, Qt.MouseButton.LeftButton, pos=QPoint(150, 150))

        assert overlay.marks == (mark,)

    def test_erase_at_returns_none_on_a_miss(self):
        overlay = self._overlay()
        overlay.add_mark(self._mark((20, 20), (80, 80)))

        assert overlay.erase_at(QPointF(150, 150)) is None
        assert len(overlay.marks) == 1

    def test_undo_erase_restores_the_mark_at_its_original_position(self):
        overlay = self._overlay()
        first = self._mark((10, 10), (30, 30))
        second = self._mark((40, 40), (60, 60))
        third = self._mark((70, 70), (90, 90))
        overlay.add_mark(first)
        overlay.add_mark(second)
        overlay.add_mark(third)

        erased = overlay.erase_at(QPointF(40, 50))  # `second`'s left border

        assert erased is second
        assert overlay.marks == (first, third)

        overlay.undo_erase()

        # Restored between `first` and `third`, its original draw-order
        # position -- not appended to the end.
        assert overlay.marks == (first, second, third)

    def test_undo_erase_is_a_no_op_with_nothing_to_restore(self):
        overlay = self._overlay()
        mark = self._mark((20, 20), (80, 80))
        overlay.add_mark(mark)

        overlay.undo_erase()  # nothing has been erased yet

        assert overlay.marks == (mark,)

    def test_cursor_is_a_pointer_over_the_selection_while_the_eraser_is_active(self):
        overlay = self._overlay(selection=QRect(50, 50, 100, 80))
        overlay.set_eraser_active(True)
        overlay.show()
        QTest.qWaitForWindowExposed(overlay)

        QTest.mouseMove(overlay, QPoint(100, 90))  # deep inside the selection

        assert overlay.cursor().shape() == Qt.CursorShape.PointingHandCursor


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
        # Not a handle, but still inside the selection: crosshair, per
        # docs/design/overlay-redesign.md's "Selection frame" cursor table
        # ("crosshair for every tool except the eraser") -- SNX-38 gives
        # this class its first non-handle cursor state.
        assert overlay.cursor().shape() == Qt.CursorShape.CrossCursor

    def test_cursor_resets_to_arrow_outside_the_selection(self):
        overlay = self._shown_overlay()

        QTest.mouseMove(overlay, QPoint(50, 50))
        assert overlay.cursor().shape() == Qt.CursorShape.SizeFDiagCursor

        QTest.mouseMove(overlay, QPoint(10, 10))  # outside the selection entirely
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


class TestUndoRedoClear:
    """SNX-39: the general undo/redo/clear stack over `_marks`, distinct
    from the eraser's own single-slot `undo_erase` (TestEraserTool above).
    """

    RED = QColor(255, 0, 0)

    def _mark(self, start, end, colour=None):
        return Rectangle(
            colour=colour or self.RED, stroke_width=4, start=QPointF(*start), end=QPointF(*end)
        )

    def _overlay(self):
        frame = make_frame(image_size=(200, 200), logical_size=(200, 200))
        overlay = OverlayWindow(frame)
        overlay.set_selection(QRect(0, 0, 200, 200))
        return overlay

    def test_undo_moves_the_newest_mark_to_the_redo_stack(self):
        overlay = self._overlay()
        first = self._mark((0, 0), (10, 10))
        second = self._mark((20, 20), (30, 30))
        overlay.add_mark(first)
        overlay.add_mark(second)

        overlay.undo()

        assert overlay.marks == (first,)
        assert overlay.can_redo

    def test_redo_returns_the_mark_to_the_same_position_in_draw_order(self):
        overlay = self._overlay()
        first = self._mark((0, 0), (10, 10))
        second = self._mark((20, 20), (30, 30))
        third = self._mark((40, 40), (50, 50))
        overlay.add_mark(first)
        overlay.add_mark(second)
        overlay.add_mark(third)

        overlay.undo()  # third -> redo
        overlay.undo()  # second -> redo
        overlay.redo()  # second back, between first and (undone) third

        assert overlay.marks == (first, second)

    def test_undo_with_nothing_to_undo_is_a_no_op(self):
        overlay = self._overlay()

        overlay.undo()

        assert overlay.marks == ()
        assert not overlay.can_undo

    def test_redo_with_nothing_to_redo_is_a_no_op(self):
        overlay = self._overlay()
        mark = self._mark((0, 0), (10, 10))
        overlay.add_mark(mark)

        overlay.redo()  # nothing has been undone

        assert overlay.marks == (mark,)

    def test_committing_a_new_mark_empties_the_redo_stack(self):
        overlay = self._overlay()
        overlay.add_mark(self._mark((0, 0), (10, 10)))
        overlay.undo()
        assert overlay.can_redo  # one mark sitting on the redo stack

        overlay.add_mark(self._mark((50, 50), (60, 60)))

        assert not overlay.can_redo
        marks_before = overlay.marks
        overlay.redo()  # no-op: the stack this would have popped is gone
        assert overlay.marks == marks_before

    def test_clear_empties_both_stacks_in_one_step(self):
        overlay = self._overlay()
        overlay.add_mark(self._mark((0, 0), (10, 10)))
        overlay.add_mark(self._mark((20, 20), (30, 30)))
        overlay.undo()  # one mark now sits on the redo stack too

        overlay.clear()

        assert overlay.marks == ()
        assert not overlay.can_undo
        assert not overlay.can_redo

    def test_clear_is_not_itself_undoable(self):
        overlay = self._overlay()
        overlay.add_mark(self._mark((0, 0), (10, 10)))

        overlay.clear()
        overlay.undo()  # must not resurrect the cleared mark

        assert overlay.marks == ()


class TestCopy:
    """SNX-39: fixes the real bug where the clipboard was written once, on
    open, before any annotation existed -- `copy()` must flatten whatever
    is in `_marks` at the moment it's called.
    """

    RED = QColor(255, 0, 0)

    def _overlay(self):
        frame = make_frame(image_size=(200, 200), logical_size=(200, 200))
        overlay = OverlayWindow(frame)
        overlay.set_selection(QRect(0, 0, 200, 200))
        return overlay

    def test_copy_puts_the_flattened_selection_on_the_clipboard(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            app_module, "copy_image_to_clipboard", lambda image: calls.append(image)
        )
        overlay = self._overlay()
        overlay.add_mark(
            Rectangle(colour=self.RED, stroke_width=6, start=QPointF(20, 20), end=QPointF(80, 80))
        )

        overlay.copy()

        assert len(calls) == 1
        copied = calls[0]
        assert isinstance(copied, QImage)
        assert copied.pixelColor(20, 50) == self.RED  # the rectangle's left border

    def test_copy_after_annotation_reflects_marks_made_since_open(self, monkeypatch):
        # The bug this ticket fixes: a real editor.py Editor copied the raw
        # capture exactly once in __init__. Calling copy() a second time,
        # after a mark lands, must pick that mark up -- not still show
        # whatever was on the clipboard when the overlay first opened.
        calls = []
        monkeypatch.setattr(
            app_module, "copy_image_to_clipboard", lambda image: calls.append(image)
        )
        overlay = self._overlay()

        overlay.copy()  # nothing drawn yet
        overlay.add_mark(
            Rectangle(colour=self.RED, stroke_width=6, start=QPointF(20, 20), end=QPointF(80, 80))
        )
        overlay.copy()  # after annotation

        assert len(calls) == 2
        before, after = calls
        assert before.pixelColor(20, 50) != self.RED
        assert after.pixelColor(20, 50) == self.RED


class TestSave:
    """SNX-39: save writes a timestamped PNG under ~/Pictures/snipux,
    creating that directory when it doesn't exist -- unlike
    `app.save_image`'s own bare-~/Pictures default, which editor.py's
    still-existing Editor keeps using.
    """

    RED = QColor(255, 0, 0)

    def _overlay(self, size=(50, 50)):
        frame = make_frame(image_size=size, logical_size=size)
        overlay = OverlayWindow(frame)
        overlay.set_selection(QRect(0, 0, *size))
        return overlay

    def test_save_writes_under_pictures_snipux_and_creates_the_directory(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setattr(app_module.Path, "home", lambda: tmp_path)
        target_dir = tmp_path / "Pictures" / "snipux"
        assert not target_dir.exists()
        overlay = self._overlay()

        path = overlay.save()

        assert path.parent == target_dir
        assert path.exists()
        assert QImage(str(path)).size() == overlay.rendered_image().size()

    def test_save_flattens_the_marks_present_at_call_time(self, monkeypatch, tmp_path):
        monkeypatch.setattr(app_module.Path, "home", lambda: tmp_path)
        overlay = self._overlay()
        overlay.add_mark(
            Rectangle(colour=self.RED, stroke_width=6, start=QPointF(5, 5), end=QPointF(40, 40))
        )

        path = overlay.save()

        saved = QImage(str(path))
        assert saved.pixelColor(5, 20) == self.RED  # the rectangle's left border


class TestFloatingBarComposition:
    """SNX-40: the bar carries the capture chip, eight tool buttons, undo,
    redo, clear, copy and save, separated by dividers, in the order
    docs/design/overlay-redesign.md's "Floating bar" table gives -- built
    from real QPushButtons rather than painted, so tooltips and hover come
    for free (TestFloatingBarTooltips below covers the tooltip half).
    """

    def test_contains_one_button_per_control_in_the_spec_table(self):
        bar = FloatingBar()

        buttons = bar.findChildren(QPushButton)

        # 1 capture chip + 8 tools + undo/redo/clear + copy/save == 14, per
        # the table's numbered rows 1, 3-10, 12-14, 16-17.
        assert len(buttons) == 14

    def test_tool_buttons_cover_every_tokens_tool_in_order(self):
        bar = FloatingBar()

        assert list(bar._tool_buttons.keys()) == tokens.TOOLS

    def test_three_dividers_separate_the_four_groups(self):
        bar = FloatingBar()

        assert len(bar.findChildren(_Divider)) == 3

    def test_undo_redo_clear_copy_save_are_all_present(self):
        bar = FloatingBar()

        assert bar._undo_button is not None
        assert bar._redo_button is not None
        assert bar._clear_button is not None
        assert bar._copy_button is not None
        assert bar._save_button is not None

    def test_bar_is_not_painted_inside_overlaywindows_paintevent(self):
        # The acceptance criterion's other half: OverlayWindow's own paint
        # pass draws the frame/scrim/ink/stroke/handles only -- see its
        # paintEvent -- and never touches `_bar` at all; the bar paints
        # itself, as a sibling layer Qt composites on top afterwards.
        frame = make_frame()
        overlay = OverlayWindow(frame)

        assert isinstance(overlay._bar, FloatingBar)
        assert overlay._bar.parent() is overlay


class TestFloatingBarFill:
    """SNX-40: 'the fill is 93% alpha, not 93% widget opacity, or the
    glyphs wash out' -- the ticket's own callout of the easy mistake.
    Verified on the rendered pixel *alpha channel*, which
    WA_TranslucentBackground preserves through `grab()` -- the same
    technique TestOverlayWindow's DIM-scrim test uses, just reading the
    alpha component instead of blending against a known backdrop.
    """

    def test_background_pixel_is_painted_at_the_token_alpha(self):
        bar = FloatingBar()
        bar.resize(bar.sizeHint())

        rendered = bar.grab().toImage()
        # Top padding strip, mid-width: inside the rounded fill but above
        # every button, so this is background only.
        pixel = rendered.pixelColor(bar.width() // 2, 2)

        expected_alpha = round(tokens.Color.BAR_BG_ALPHA * 255)
        assert pixel.alpha() == pytest.approx(expected_alpha, abs=2)
        assert (pixel.red(), pixel.green(), pixel.blue()) == QColor(
            tokens.Color.BAR_BG
        ).getRgb()[:3]

    def test_glyph_pixels_stay_fully_opaque_over_the_translucent_fill(self):
        # Painting the whole widget at reduced *opacity* (the mistake the
        # README warns about) would leave every glyph pixel translucent
        # too, at the same ~237/255 alpha as the background. Scans the pen
        # button's whole rect rather than one predicted pixel, since the
        # icon is a stroke outline -- most of the button is transparent
        # background, and only the stroke itself needs to prove opaque.
        # A tool button, not undo/redo: those start disabled, and a
        # disabled glyph is deliberately its own (still opaque, just
        # different-coloured) case -- see TestFloatingBarUndoRedo below.
        bar = FloatingBar()
        bar.resize(bar.sizeHint())

        rendered = bar.grab().toImage()
        rect = bar._tool_buttons["pen"].geometry()
        alphas = [
            rendered.pixelColor(x, y).alpha()
            for x in range(rect.left(), rect.right())
            for y in range(rect.top(), rect.bottom())
        ]

        assert max(alphas) == 255


class TestFloatingBarPositioning:
    """SNX-40: 'the bar is centred under the selection and clamped so it
    stays fully on screen when the selection is dragged low', per the
    spec's "Floating bar" clamp rule.
    """

    def test_centres_under_the_selection_with_room_to_spare(self):
        bar = FloatingBar()
        selection = QRect(400, 200, 200, 150)  # bottom edge at y=350
        bounds = QSize(1600, 1000)

        bar.reposition(selection, bounds)

        assert bar.geometry().center().x() == pytest.approx(
            selection.center().x(), abs=1
        )
        # QRectF, not QRect.bottom(): the latter is inclusive
        # (top + height - 1), the same one-pixel trap `_bracket_path`
        # documents elsewhere in overlay.py.
        expected_top = QRectF(selection).bottom() + tokens.Metric.BAR_OFFSET_Y
        assert bar.geometry().top() == expected_top

    def test_top_clamps_so_the_bar_cannot_leave_a_short_window(self):
        bounds = QSize(1600, 400)
        # Natural position (bottom + BAR_OFFSET_Y) would land past the
        # window's own bottom edge.
        selection = QRect(400, 350, 200, 40)
        bar = FloatingBar()

        bar.reposition(selection, bounds)

        assert bar.geometry().top() == bounds.height() - FloatingBar._TOP_MAX_FROM_BOTTOM
        assert bar.geometry().bottom() <= bounds.height()

    def test_centre_clamps_away_from_the_left_screen_edge(self):
        bar = FloatingBar()
        bounds = QSize(1600, 1000)
        selection = QRect(0, 200, 50, 50)  # centre x = 25, far left

        bar.reposition(selection, bounds)

        # abs=1: the bar's own width (sizeHint) is odd, so an exact
        # BAR_MIN_EDGE centre can land the integer geometry a pixel off
        # either side of it -- the same rounding TestReframing tolerates
        # elsewhere in this file for the same reason.
        assert bar.geometry().center().x() == pytest.approx(tokens.Metric.BAR_MIN_EDGE, abs=1)

    def test_centre_clamps_away_from_the_right_screen_edge(self):
        bar = FloatingBar()
        bounds = QSize(1600, 1000)
        selection = QRect(1580, 200, 15, 50)  # centre x near the right edge

        bar.reposition(selection, bounds)

        expected = bounds.width() - tokens.Metric.BAR_MIN_EDGE
        assert bar.geometry().center().x() == pytest.approx(expected, abs=1)


class TestFloatingBarActiveTool:
    """SNX-40: 'exactly one tool reads as active at a time.'"""

    def test_clicking_a_tool_makes_it_the_only_active_one(self):
        bar = FloatingBar()

        QTest.mouseClick(bar._tool_buttons["pen"], Qt.MouseButton.LeftButton)

        assert bar.active_tool == "pen"
        assert bar._tool_buttons["pen"].is_active
        assert all(
            not button.is_active
            for name, button in bar._tool_buttons.items()
            if name != "pen"
        )

    def test_selecting_a_second_tool_deactivates_the_first(self):
        bar = FloatingBar()
        QTest.mouseClick(bar._tool_buttons["pen"], Qt.MouseButton.LeftButton)

        QTest.mouseClick(bar._tool_buttons["arrow"], Qt.MouseButton.LeftButton)

        assert bar.active_tool == "arrow"
        assert not bar._tool_buttons["pen"].is_active
        assert bar._tool_buttons["arrow"].is_active

    def test_clicking_a_tool_emits_tool_selected(self):
        bar = FloatingBar()
        received = Mock()
        bar.toolSelected.connect(received)

        QTest.mouseClick(bar._tool_buttons["blur"], Qt.MouseButton.LeftButton)

        received.assert_called_once_with("blur")


class TestFloatingBarUndoRedo:
    """SNX-40: 'undo and redo take the disabled colour when their stack is
    empty' -- implemented as a real QWidget.setEnabled(False), per the
    README's own "disabled is better" preference (see _IconButton).
    """

    def test_undo_and_redo_start_disabled(self):
        bar = FloatingBar()

        assert not bar._undo_button.isEnabled()
        assert not bar._redo_button.isEnabled()

    def test_set_undo_enabled_toggles_the_button(self):
        bar = FloatingBar()

        bar.set_undo_enabled(True)
        assert bar._undo_button.isEnabled()

        bar.set_undo_enabled(False)
        assert not bar._undo_button.isEnabled()

    def test_set_redo_enabled_toggles_the_button(self):
        bar = FloatingBar()

        bar.set_redo_enabled(True)
        assert bar._redo_button.isEnabled()

    def test_disabled_undo_glyph_uses_the_disabled_token_colour(self):
        # Checked on the button's own QIcon rather than a full bar grab():
        # a raster SVG this small never quite reaches alpha==255 at any one
        # pixel (soft antialiasing on a 1.55px stroke), so the strongest
        # -coverage pixel -- not a fully-opaque one -- is the closest this
        # icon gets to "solid", and that is enough to prove which colour it
        # was painted, independent of whatever the bar composites it over.
        bar = FloatingBar()

        pixmap = bar._undo_button.icon().pixmap(
            tokens.Metric.ICON, tokens.Metric.ICON, QIcon.Mode.Disabled
        )
        image = pixmap.toImage()
        strongest = max(
            (image.pixelColor(x, y) for x in range(image.width()) for y in range(image.height())),
            key=lambda c: c.alpha(),
        )

        expected = QColor(tokens.Color.ICON_DISABLED)
        assert strongest.red() == pytest.approx(expected.red(), abs=2)
        assert strongest.green() == pytest.approx(expected.green(), abs=2)
        assert strongest.blue() == pytest.approx(expected.blue(), abs=2)


class TestFloatingBarTooltips:
    """SNX-40: 'each button has a tooltip naming the control and its
    shortcut.'
    """

    @pytest.mark.parametrize("tool", tokens.TOOLS)
    def test_tool_button_tooltip_names_the_control_and_its_shortcut(self, tool):
        bar = FloatingBar()

        tooltip = bar._tool_buttons[tool].toolTip()

        assert tooltip == f"{_tool_label(tool)} — {_TOOL_SHORTCUT_KEYS[tool]}"

    def test_undo_tooltip_names_the_control_and_its_shortcut(self):
        bar = FloatingBar()

        assert bar._undo_button.toolTip() == f"Undo — {FloatingBar.UNDO_SHORTCUT}"

    def test_redo_tooltip_names_the_control_and_its_shortcut(self):
        bar = FloatingBar()

        assert bar._redo_button.toolTip() == f"Redo — {FloatingBar.REDO_SHORTCUT}"

    def test_every_button_has_a_non_empty_tooltip(self):
        bar = FloatingBar()

        for button in bar.findChildren(QPushButton):
            assert button.toolTip()


class TestFloatingBarIntegration:
    """SNX-40: the bar wired into `OverlayWindow` -- a real child widget
    positioned under the live selection, driving the same
    undo/redo/clear/copy/save/eraser API SNX-38/39 already built.
    """

    RED = QColor(255, 0, 0)

    def _overlay(self, size=(1600, 1000)):
        frame = make_frame(image_size=size, logical_size=size)
        return OverlayWindow(frame)

    def test_bar_becomes_visible_and_positioned_once_the_overlay_is_shown(self):
        overlay = self._overlay()
        overlay.show()
        QTest.qWaitForWindowExposed(overlay)

        selection = QRect(400, 200, 200, 150)
        overlay.set_selection(selection)

        assert overlay._bar.isVisible()
        # QRectF, not selection.bottom(): QRect.bottom() is inclusive
        # (top + height - 1), same one-pixel trap `_bracket_path` already
        # documents elsewhere in overlay.py.
        expected_top = QRectF(selection).bottom() + tokens.Metric.BAR_OFFSET_Y
        assert overlay._bar.geometry().top() == expected_top

    def test_bar_hides_again_once_the_selection_is_cleared(self):
        overlay = self._overlay()
        overlay.show()
        QTest.qWaitForWindowExposed(overlay)
        overlay.set_selection(QRect(400, 200, 200, 150))
        assert overlay._bar.isVisible()

        overlay.set_selection(None)

        assert not overlay._bar.isVisible()

    def test_bar_stays_hidden_and_unpainted_while_the_overlay_itself_is_not_shown(self):
        # None of this file's other OverlayWindow pixel tests ever call
        # .show() before grab()ing -- this is the guarantee that keeps the
        # bar from starting to paint over whatever they sample once it
        # exists as a real child widget.
        overlay = self._overlay(size=(200, 200))

        overlay.set_selection(QRect(50, 50, 50, 50))

        assert not overlay._bar.isVisible()

    def test_undo_button_click_undoes_the_newest_mark(self):
        overlay = self._overlay()
        overlay.set_selection(QRect(0, 0, 1600, 1000))
        overlay.add_mark(
            Rectangle(
                colour=self.RED, stroke_width=4, start=QPointF(10, 10), end=QPointF(30, 30)
            )
        )
        assert overlay._bar._undo_button.isEnabled()

        QTest.mouseClick(overlay._bar._undo_button, Qt.MouseButton.LeftButton)

        assert overlay.marks == ()
        assert not overlay._bar._undo_button.isEnabled()

    def test_redo_button_click_restores_the_undone_mark(self):
        overlay = self._overlay()
        overlay.set_selection(QRect(0, 0, 1600, 1000))
        mark = Rectangle(
            colour=self.RED, stroke_width=4, start=QPointF(10, 10), end=QPointF(30, 30)
        )
        overlay.add_mark(mark)
        overlay.undo()
        assert overlay._bar._redo_button.isEnabled()

        QTest.mouseClick(overlay._bar._redo_button, Qt.MouseButton.LeftButton)

        assert overlay.marks == (mark,)

    def test_clear_button_click_empties_the_ink_layer(self):
        overlay = self._overlay()
        overlay.set_selection(QRect(0, 0, 1600, 1000))
        overlay.add_mark(
            Rectangle(
                colour=self.RED, stroke_width=4, start=QPointF(10, 10), end=QPointF(30, 30)
            )
        )

        QTest.mouseClick(overlay._bar._clear_button, Qt.MouseButton.LeftButton)

        assert overlay.marks == ()

    def test_clicking_the_eraser_tool_button_arms_the_eraser(self):
        overlay = self._overlay()

        QTest.mouseClick(overlay._bar._tool_buttons["eraser"], Qt.MouseButton.LeftButton)

        assert overlay._eraser_active

    def test_clicking_a_different_tool_disarms_the_eraser(self):
        overlay = self._overlay()
        QTest.mouseClick(overlay._bar._tool_buttons["eraser"], Qt.MouseButton.LeftButton)
        assert overlay._eraser_active

        QTest.mouseClick(overlay._bar._tool_buttons["pen"], Qt.MouseButton.LeftButton)

        assert not overlay._eraser_active

    def test_copy_button_click_copies_the_current_marks(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            app_module, "copy_image_to_clipboard", lambda image: calls.append(image)
        )
        overlay = self._overlay(size=(200, 200))
        overlay.set_selection(QRect(0, 0, 200, 200))

        QTest.mouseClick(overlay._bar._copy_button, Qt.MouseButton.LeftButton)

        assert len(calls) == 1

    def test_save_button_click_writes_a_file(self, monkeypatch, tmp_path):
        monkeypatch.setattr(app_module.Path, "home", lambda: tmp_path)
        overlay = self._overlay(size=(50, 50))
        overlay.set_selection(QRect(0, 0, 50, 50))

        QTest.mouseClick(overlay._bar._save_button, Qt.MouseButton.LeftButton)

        assert (tmp_path / "Pictures" / "snipux").exists()


class TestSettingsTrayVisibility:
    """SNX-41: 'colour and stroke are not controls until the user is
    holding something that draws' -- the tray's whole reason for existing.
    """

    @pytest.mark.parametrize("tool", tokens.DRAW_TOOLS)
    def test_shown_for_every_draw_tool(self, tool):
        tray = SettingsTray()

        tray.set_tool(tool)

        assert tray.isVisible()

    def test_hidden_for_the_eraser(self):
        tray = SettingsTray()
        tray.set_tool("pen")
        assert tray.isVisible()

        tray.set_tool("eraser")

        assert not tray.isVisible()

    def test_hidden_for_blur(self):
        # Blur gets its own strength/mode tray (BlurTray, SNX-42) in this
        # one's place -- 'blur' is simply not a tokens.DRAW_TOOLS member,
        # so this tray hides for it exactly like the eraser.
        tray = SettingsTray()
        tray.set_tool("pen")

        tray.set_tool("blur")

        assert not tray.isVisible()


class TestSettingsTrayComposition:
    """SNX-41: the tray carries the active-tool pill, the ink swatches, a
    custom-colour button, a stroke slider, a readout, a preview dot and a
    hint -- separated by dividers, per the spec's "Settings tray" table.
    """

    def test_contains_one_swatch_per_ink_swatch_token(self):
        tray = SettingsTray()

        assert list(tray._swatch_buttons.keys()) == [hex for _name, hex in tokens.INK_SWATCHES]

    def test_contains_the_custom_colour_button(self):
        tray = SettingsTray()

        assert isinstance(tray._custom_button, _CustomColorButton)

    def test_three_dividers_separate_the_four_groups(self):
        tray = SettingsTray()

        assert len(tray.findChildren(_Divider)) == 3

    def test_contains_the_slider_readout_and_preview_dot(self):
        tray = SettingsTray()

        assert isinstance(tray._slider, QSlider)
        assert tray._readout is not None
        assert isinstance(tray._preview, _PreviewDot)

    def test_pill_names_the_active_tool(self):
        tray = SettingsTray()

        tray.set_tool("arrow")

        assert tray._pill._text_label.text() == _tool_label("arrow")

    def test_hint_matches_the_active_tools_token_hint(self):
        tray = SettingsTray()

        tray.set_tool("rect")

        assert tray._hint.text() == tokens.TOOL_HINTS["rect"]

    def test_slider_range_matches_the_stroke_tokens(self):
        tray = SettingsTray()

        assert tray._slider.minimum() == tokens.Metric.STROKE_MIN
        assert tray._slider.maximum() == tokens.Metric.STROKE_MAX

    def test_default_stroke_matches_the_token_default(self):
        tray = SettingsTray()

        assert tray.stroke == tokens.Metric.STROKE_DEFAULT
        assert tray._slider.value() == tokens.Metric.STROKE_DEFAULT

    def test_default_colour_is_the_first_ink_swatch(self):
        tray = SettingsTray()

        assert tray.colour == tokens.INK_SWATCHES[0][1]


class TestSettingsTraySwatchSelection:
    """SNX-41: 'the selected swatch is drawn with the double ring the spec
    describes, and picking one changes the colour new marks are drawn in.'
    """

    def test_default_swatch_is_selected(self):
        tray = SettingsTray()

        default_hex = tokens.INK_SWATCHES[0][1]
        assert tray._swatch_buttons[default_hex].is_selected
        assert all(
            not button.is_selected
            for hex_colour, button in tray._swatch_buttons.items()
            if hex_colour != default_hex
        )

    def test_clicking_a_swatch_selects_it_and_deselects_the_rest(self):
        tray = SettingsTray()
        _name, target_hex = tokens.INK_SWATCHES[2]

        QTest.mouseClick(tray._swatch_buttons[target_hex], Qt.MouseButton.LeftButton)

        assert tray._swatch_buttons[target_hex].is_selected
        assert tray.colour == target_hex
        assert all(
            not button.is_selected
            for hex_colour, button in tray._swatch_buttons.items()
            if hex_colour != target_hex
        )

    def test_clicking_a_swatch_emits_colour_changed(self):
        tray = SettingsTray()
        received = Mock()
        tray.colourChanged.connect(received)
        _name, target_hex = tokens.INK_SWATCHES[1]

        QTest.mouseClick(tray._swatch_buttons[target_hex], Qt.MouseButton.LeftButton)

        received.assert_called_once_with(target_hex)

    def test_unselected_swatch_paints_only_its_own_flat_colour(self):
        # No ring at all for a swatch that isn't the selected one -- this
        # is the negative case the double-ring test below leans on.
        tray = SettingsTray()
        _name, hex_colour = tokens.INK_SWATCHES[3]
        button = tray._swatch_buttons[hex_colour]
        button.resize(button.sizeHint())

        rendered = button.grab().toImage()
        center = rendered.pixelColor(button.width() // 2, button.height() // 2)

        assert (center.red(), center.green(), center.blue()) == QColor(hex_colour).getRgb()[:3]

    def test_selected_swatch_paints_the_double_ring(self):
        tray = SettingsTray()
        _name, hex_colour = tokens.INK_SWATCHES[0]
        button = tray._swatch_buttons[hex_colour]
        button.set_selected(True)
        button.resize(button.sizeHint())

        rendered = button.grab().toImage()
        center = rendered.pixelColor(button.width() // 2, button.height() // 2)
        # The outermost pixel: the light ring painted flush against the
        # button's own edge, at mid-height so it falls on the ring's flat
        # side rather than its rounded corner.
        edge = rendered.pixelColor(0, button.height() // 2)

        assert (center.red(), center.green(), center.blue()) == QColor(hex_colour).getRgb()[:3]
        assert (edge.red(), edge.green(), edge.blue()) == QColor(
            tokens.Color.TEXT_PRIMARY
        ).getRgb()[:3]


class TestSettingsTrayCustomColour:
    """SNX-41: 'the custom-colour button opens a colour dialog and the
    colour it returns becomes the current ink colour.'
    """

    def test_click_opens_the_colour_dialog_seeded_with_the_current_colour(self, monkeypatch):
        tray = SettingsTray()
        seen = []
        monkeypatch.setattr(
            QColorDialog,
            "getColor",
            staticmethod(lambda initial, *a, **k: seen.append(initial) or QColor()),
        )

        QTest.mouseClick(tray._custom_button, Qt.MouseButton.LeftButton)

        assert seen == [QColor(tray.colour)]

    def test_a_valid_returned_colour_becomes_the_current_ink_colour(self, monkeypatch):
        tray = SettingsTray()
        chosen = QColor("#336699")
        monkeypatch.setattr(QColorDialog, "getColor", staticmethod(lambda *a, **k: chosen))

        QTest.mouseClick(tray._custom_button, Qt.MouseButton.LeftButton)

        assert tray.colour == chosen.name()
        # No swatch reads as selected once the colour is a custom one that
        # doesn't match any of them.
        assert not any(button.is_selected for button in tray._swatch_buttons.values())

    def test_cancelling_the_dialog_leaves_the_colour_unchanged(self, monkeypatch):
        tray = SettingsTray()
        original = tray.colour
        # QColorDialog.getColor() returns an invalid QColor on Cancel.
        monkeypatch.setattr(QColorDialog, "getColor", staticmethod(lambda *a, **k: QColor()))

        QTest.mouseClick(tray._custom_button, Qt.MouseButton.LeftButton)

        assert tray.colour == original


class TestSettingsTrayStrokeReadout:
    """SNX-41: 'the stroke readout has a minimum width so the tray does
    not change width as the number does.'
    """

    def test_readout_has_the_specs_minimum_width(self):
        tray = SettingsTray()

        assert tray._readout.minimumWidth() == SettingsTray._READOUT_MIN_W

    def test_readout_shows_the_default_stroke(self):
        tray = SettingsTray()

        assert tray._readout.text() == f"{tokens.Metric.STROKE_DEFAULT}px"

    def test_moving_the_slider_updates_the_readout(self):
        tray = SettingsTray()

        tray._slider.setValue(17)

        assert tray._readout.text() == "17px"
        assert tray.stroke == 17

    def test_moving_the_slider_emits_stroke_changed(self):
        tray = SettingsTray()
        received = Mock()
        tray.strokeChanged.connect(received)

        tray._slider.setValue(9)

        received.assert_called_once_with(9)

    def test_set_stroke_clamps_to_the_token_range(self):
        tray = SettingsTray()

        tray.set_stroke(tokens.Metric.STROKE_MAX + 50)
        assert tray.stroke == tokens.Metric.STROKE_MAX

        tray.set_stroke(tokens.Metric.STROKE_MIN - 50)
        assert tray.stroke == tokens.Metric.STROKE_MIN


class TestSettingsTrayPreviewDot:
    """SNX-41: 'the preview dot shows the current colour at the current
    stroke, multiplied for the highlighter and clamped to the token
    range.'
    """

    def test_preview_matches_colour_and_stroke_for_a_plain_tool(self):
        tray = SettingsTray()
        tray.set_tool("pen")

        tray.set_colour(tokens.INK_SWATCHES[4][1])
        tray.set_stroke(10)

        assert tray._preview._colour == QColor(tokens.INK_SWATCHES[4][1])
        assert tray._preview._diameter == 10

    def test_preview_diameter_is_multiplied_for_the_highlighter(self):
        tray = SettingsTray()
        tray.set_tool("highlighter")

        tray.set_stroke(4)

        assert tray._preview._diameter == pytest.approx(4 * tokens.Metric.HIGHLIGHT_MULT)

    def test_preview_diameter_is_clamped_to_the_stroke_token_range(self):
        tray = SettingsTray()
        tray.set_tool("highlighter")

        # 4 * HIGHLIGHT_MULT (3.5) == 14, well inside range; a stroke near
        # the top of the range multiplied by 3.5 blows past STROKE_MAX and
        # must clamp down to it rather than overflowing the 28px box.
        tray.set_stroke(tokens.Metric.STROKE_MAX)

        assert tray._preview._diameter == tokens.Metric.STROKE_MAX

    def test_switching_back_to_a_plain_tool_drops_the_multiplier(self):
        tray = SettingsTray()
        tray.set_tool("highlighter")
        tray.set_stroke(6)
        assert tray._preview._diameter == pytest.approx(6 * tokens.Metric.HIGHLIGHT_MULT)

        tray.set_tool("pen")

        assert tray._preview._diameter == 6


class TestSettingsTrayOverlayIntegration:
    """SNX-41: the tray wired into `OverlayWindow`, positioned under the
    bar and shown only while the bar's active tool draws -- mirroring how
    SNX-40 wired `FloatingBar` in.
    """

    def _overlay(self, size=(1600, 1000)):
        frame = make_frame(image_size=size, logical_size=size)
        return OverlayWindow(frame)

    def test_tray_shown_and_positioned_once_a_draw_tool_is_picked(self):
        overlay = self._overlay()
        overlay.show()
        QTest.qWaitForWindowExposed(overlay)
        overlay.set_selection(QRect(400, 200, 200, 150))

        QTest.mouseClick(overlay._bar._tool_buttons["pen"], Qt.MouseButton.LeftButton)

        assert overlay._tray.isVisible()
        expected_top = overlay._bar.geometry().bottom() + tokens.Metric.TRAY_OFFSET_Y
        assert overlay._tray.geometry().top() == expected_top
        # abs=1: the bar's and tray's own sizeHint widths can differ in
        # parity, the same one-pixel rounding TestFloatingBarPositioning
        # already tolerates for the same reason.
        assert overlay._tray.geometry().center().x() == pytest.approx(
            overlay._bar.geometry().center().x(), abs=1
        )

    def test_tray_hidden_for_the_eraser(self):
        overlay = self._overlay()
        overlay.show()
        QTest.qWaitForWindowExposed(overlay)
        overlay.set_selection(QRect(400, 200, 200, 150))
        QTest.mouseClick(overlay._bar._tool_buttons["pen"], Qt.MouseButton.LeftButton)
        assert overlay._tray.isVisible()

        QTest.mouseClick(overlay._bar._tool_buttons["eraser"], Qt.MouseButton.LeftButton)

        assert not overlay._tray.isVisible()

    def test_tray_hides_when_the_selection_is_cleared(self):
        overlay = self._overlay()
        overlay.show()
        QTest.qWaitForWindowExposed(overlay)
        overlay.set_selection(QRect(400, 200, 200, 150))
        QTest.mouseClick(overlay._bar._tool_buttons["pen"], Qt.MouseButton.LeftButton)
        assert overlay._tray.isVisible()

        overlay.set_selection(None)

        assert not overlay._tray.isVisible()

    def test_tray_stays_hidden_while_the_overlay_itself_is_not_shown(self):
        # Same guarantee TestFloatingBarIntegration establishes for `_bar`:
        # none of this file's pixel-sampling OverlayWindow tests call
        # .show(), so neither child widget may start painting into a
        # grab() they didn't ask for.
        overlay = self._overlay(size=(200, 200))
        overlay.set_selection(QRect(50, 50, 50, 50))

        overlay._on_tool_selected("pen")

        assert not overlay._tray.isVisible()

    def test_picking_a_swatch_updates_the_overlays_ink_colour(self):
        overlay = self._overlay()
        overlay.show()
        QTest.qWaitForWindowExposed(overlay)
        overlay.set_selection(QRect(400, 200, 200, 150))
        QTest.mouseClick(overlay._bar._tool_buttons["pen"], Qt.MouseButton.LeftButton)
        _name, target_hex = tokens.INK_SWATCHES[3]

        QTest.mouseClick(overlay._tray._swatch_buttons[target_hex], Qt.MouseButton.LeftButton)

        assert overlay._ink_colour == target_hex

    def test_moving_the_stroke_slider_updates_the_overlays_stroke_width(self):
        overlay = self._overlay()
        overlay.show()
        QTest.qWaitForWindowExposed(overlay)
        overlay.set_selection(QRect(400, 200, 200, 150))
        QTest.mouseClick(overlay._bar._tool_buttons["pen"], Qt.MouseButton.LeftButton)

        overlay._tray._slider.setValue(21)

        assert overlay._stroke_width == 21


class TestBlurTrayComposition:
    """SNX-42: the blur tray replaces SettingsTray's colour/stroke controls
    with a Blur/Pixelate toggle, a strength slider/readout and the hint --
    per docs/design/overlay-redesign.md's "Blur tray" paragraph.
    """

    def test_contains_the_mode_well_with_both_segments(self):
        tray = BlurTray()

        assert isinstance(tray._well, _BlurModeWell)
        assert isinstance(tray._well.blur_button, _SegmentButton)
        assert isinstance(tray._well.pixelate_button, _SegmentButton)

    def test_contains_the_slider_and_readout(self):
        tray = BlurTray()

        assert isinstance(tray._slider, QSlider)
        assert tray._readout is not None

    def test_hint_matches_the_blur_tools_token_hint(self):
        tray = BlurTray()

        assert tray._hint.text() == tokens.TOOL_HINTS["blur"]

    def test_slider_range_matches_the_blur_tokens(self):
        tray = BlurTray()

        assert tray._slider.minimum() == tokens.Metric.BLUR_MIN
        assert tray._slider.maximum() == tokens.Metric.BLUR_MAX

    def test_default_strength_matches_the_token_default(self):
        tray = BlurTray()

        assert tray.strength == tokens.Metric.BLUR_DEFAULT
        assert tray._slider.value() == tokens.Metric.BLUR_DEFAULT

    def test_default_blur_mode_is_blur(self):
        tray = BlurTray()

        assert tray.blur_mode == "blur"


class TestBlurTraySegmentToggle:
    """SNX-42: 'the two-segment toggle chooses between blur and pixelate,
    and exactly one segment reads as active,' and 'the segment that is
    active decides which obscuring shape a drag commits.'
    """

    def test_blur_segment_is_active_by_default(self):
        tray = BlurTray()

        assert tray._well.blur_button.is_active
        assert not tray._well.pixelate_button.is_active

    def test_clicking_pixelate_activates_it_and_deactivates_blur(self):
        tray = BlurTray()

        QTest.mouseClick(tray._well.pixelate_button, Qt.MouseButton.LeftButton)

        assert tray.blur_mode == "pix"
        assert tray._well.pixelate_button.is_active
        assert not tray._well.blur_button.is_active

    def test_clicking_blur_after_pixelate_activates_it_and_deactivates_pixelate(self):
        tray = BlurTray()
        QTest.mouseClick(tray._well.pixelate_button, Qt.MouseButton.LeftButton)

        QTest.mouseClick(tray._well.blur_button, Qt.MouseButton.LeftButton)

        assert tray.blur_mode == "blur"
        assert tray._well.blur_button.is_active
        assert not tray._well.pixelate_button.is_active

    def test_clicking_a_segment_emits_blur_mode_changed(self):
        tray = BlurTray()
        received = Mock()
        tray.blurModeChanged.connect(received)

        QTest.mouseClick(tray._well.pixelate_button, Qt.MouseButton.LeftButton)

        received.assert_called_once_with("pix")


class TestBlurTrayStrengthReadout:
    """SNX-42: 'the strength slider covers the range in tokens.py and
    starts at the token default' and 'the readout shows the current
    strength and has a minimum width so the tray does not reflow.'
    """

    def test_readout_has_a_minimum_width(self):
        tray = BlurTray()

        assert tray._readout.minimumWidth() == BlurTray._READOUT_MIN_W

    def test_readout_shows_the_default_strength(self):
        tray = BlurTray()

        assert tray._readout.text() == str(tokens.Metric.BLUR_DEFAULT)

    def test_moving_the_slider_updates_the_readout(self):
        tray = BlurTray()

        tray._slider.setValue(15)

        assert tray._readout.text() == "15"
        assert tray.strength == 15

    def test_moving_the_slider_emits_strength_changed(self):
        tray = BlurTray()
        received = Mock()
        tray.strengthChanged.connect(received)

        tray._slider.setValue(12)

        received.assert_called_once_with(12)

    def test_set_strength_clamps_to_the_token_range(self):
        tray = BlurTray()

        tray.set_strength(tokens.Metric.BLUR_MAX + 50)
        assert tray.strength == tokens.Metric.BLUR_MAX

        tray.set_strength(tokens.Metric.BLUR_MIN - 50)
        assert tray.strength == tokens.Metric.BLUR_MIN


class TestBlurTrayOverlayIntegration:
    """SNX-42: the blur tray wired into OverlayWindow, shown in place of
    SettingsTray -- never alongside it -- while the bar's active tool is
    'blur', mirroring how SNX-41 wired SettingsTray in.
    """

    def _overlay(self, size=(1600, 1000)):
        frame = make_frame(image_size=size, logical_size=size)
        return OverlayWindow(frame)

    def test_blur_tray_shown_and_positioned_once_blur_is_picked(self):
        overlay = self._overlay()
        overlay.show()
        QTest.qWaitForWindowExposed(overlay)
        overlay.set_selection(QRect(400, 200, 200, 150))

        QTest.mouseClick(overlay._bar._tool_buttons["blur"], Qt.MouseButton.LeftButton)

        assert overlay._blur_tray.isVisible()
        assert not overlay._tray.isVisible()
        expected_top = overlay._bar.geometry().bottom() + tokens.Metric.TRAY_OFFSET_Y
        assert overlay._blur_tray.geometry().top() == expected_top
        assert overlay._blur_tray.geometry().center().x() == pytest.approx(
            overlay._bar.geometry().center().x(), abs=1
        )

    def test_draw_tray_replaces_blur_tray_when_switching_back(self):
        overlay = self._overlay()
        overlay.show()
        QTest.qWaitForWindowExposed(overlay)
        overlay.set_selection(QRect(400, 200, 200, 150))
        QTest.mouseClick(overlay._bar._tool_buttons["blur"], Qt.MouseButton.LeftButton)
        assert overlay._blur_tray.isVisible()

        QTest.mouseClick(overlay._bar._tool_buttons["pen"], Qt.MouseButton.LeftButton)

        assert overlay._tray.isVisible()
        assert not overlay._blur_tray.isVisible()

    def test_blur_tray_hidden_for_the_eraser(self):
        overlay = self._overlay()
        overlay.show()
        QTest.qWaitForWindowExposed(overlay)
        overlay.set_selection(QRect(400, 200, 200, 150))
        QTest.mouseClick(overlay._bar._tool_buttons["blur"], Qt.MouseButton.LeftButton)
        assert overlay._blur_tray.isVisible()

        QTest.mouseClick(overlay._bar._tool_buttons["eraser"], Qt.MouseButton.LeftButton)

        assert not overlay._blur_tray.isVisible()
        assert not overlay._tray.isVisible()

    def test_blur_tray_hides_when_the_selection_is_cleared(self):
        overlay = self._overlay()
        overlay.show()
        QTest.qWaitForWindowExposed(overlay)
        overlay.set_selection(QRect(400, 200, 200, 150))
        QTest.mouseClick(overlay._bar._tool_buttons["blur"], Qt.MouseButton.LeftButton)
        assert overlay._blur_tray.isVisible()

        overlay.set_selection(None)

        assert not overlay._blur_tray.isVisible()

    def test_blur_tray_stays_hidden_while_the_overlay_itself_is_not_shown(self):
        # Same guarantee TestSettingsTrayOverlayIntegration establishes for
        # `_tray`: none of this file's pixel-sampling OverlayWindow tests
        # call .show(), so `_blur_tray` may not start painting into a
        # grab() it wasn't asked for either.
        overlay = self._overlay(size=(200, 200))
        overlay.set_selection(QRect(50, 50, 50, 50))

        overlay._on_tool_selected("blur")

        assert not overlay._blur_tray.isVisible()

    def test_toggling_the_segment_updates_the_overlays_blur_mode(self):
        overlay = self._overlay()
        overlay.show()
        QTest.qWaitForWindowExposed(overlay)
        overlay.set_selection(QRect(400, 200, 200, 150))
        QTest.mouseClick(overlay._bar._tool_buttons["blur"], Qt.MouseButton.LeftButton)
        assert overlay._blur_mode == "blur"

        QTest.mouseClick(overlay._blur_tray._well.pixelate_button, Qt.MouseButton.LeftButton)

        assert overlay._blur_mode == "pix"

    def test_moving_the_strength_slider_updates_the_overlays_blur_strength(self):
        overlay = self._overlay()
        overlay.show()
        QTest.qWaitForWindowExposed(overlay)
        overlay.set_selection(QRect(400, 200, 200, 150))
        QTest.mouseClick(overlay._bar._tool_buttons["blur"], Qt.MouseButton.LeftButton)

        overlay._blur_tray._slider.setValue(17)

        assert overlay._blur_strength == 17
