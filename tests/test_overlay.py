import ctypes
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PyQt6.QtCore import QPoint, QPointF, QRect, QRectF, QSize, QSizeF, Qt
from PyQt6.QtGui import (
    QColor,
    QFont,
    QFontMetricsF,
    QGuiApplication,
    QIcon,
    QImage,
    QPainter,
    QPainterPath,
    qRgb,
)
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import (
    QApplication,
    QColorDialog,
    QLabel,
    QLineEdit,
    QPushButton,
    QSlider,
    QWidget,
)

import snipux.app as app_module
import snipux.overlay as overlay_module
from conftest import skip_on_windows
from snipux import capture as capture_module
from snipux import setup_desktop
from snipux.scroll import ScrollCaptureError
from snipux.capture import (
    BackendRegistry,
    CaptureBackend,
    Frame,
    WindowsWindowGeometryProvider,
    X11WindowGeometryProvider,
)
from snipux.design import color as design_color
from snipux.design import font_families
from snipux.design import tokens
from snipux.shapes import (
    Arrow,
    Blur,
    Crop,
    Ellipse,
    Highlighter,
    Line,
    ObscuringShape,
    Pen,
    Pixelate,
    Rectangle,
    StepMarker,
    Text,
)
from snipux.overlay import (
    BlurTray,
    CaptureModePopover,
    DelayCountdown,
    FloatingBar,
    GeometryProvider,
    Handle,
    HintHUD,
    Overlay,
    OverlayWindow,
    SelectionMode,
    SettingsTray,
    ShapeToolPopover,
    Toast,
    UnsupportedGeometryProvider,
    _BlurModeWell,
    _CaptureModeRow,
    _CustomColorButton,
    _DelayRow,
    _Divider,
    _HANDLE_CURSORS,
    _MenuSeparator,
    _PillButton,
    _PreviewDot,
    _SegmentButton,
    _SwatchButton,
    _TOOL_SHORTCUT_KEYS,
    _ToolPill,
    _tool_label,
    create_overlays,
    open_overlay,
)

BASE_COLOR = qRgb(10, 20, 30)


def pixel(image, x, y=None):
    """Sample `image` at a *logical* point.

    `QWidget.grab()` hands back a pixmap at the display's device pixel
    ratio, so on a fractionally-scaled display a logical point is not an
    image index: at 1.5x, logical (100, 100) is image pixel (150, 150).
    Every pixel assertion in this file is written in the logical
    coordinates the painting code itself works in, and reading them
    straight off the grabbed image is what pinned the whole file to a
    scale factor of 1.0 -- 26 tests here failed under QT_SCALE_FACTOR=1.5
    while the painting they check was provably correct.

    Safe on a plain `QImage` too, which reports a ratio of 1.0 and so
    passes the coordinates through untouched. That is why the handful of
    reads against source frames (`frame.image`, `frame.crop(...).image`)
    are left indexing directly: they are never grabbed, so there is
    nothing to convert.
    """
    ratio = image.devicePixelRatio()
    if y is None:  # the QPoint overload this file also uses
        return image.pixelColor(round(x.x() * ratio), round(x.y() * ratio))
    return image.pixelColor(round(x * ratio), round(y * ratio))


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


def make_gradient_frame(size=(200, 200)) -> Frame:
    # A flat make_frame() fill would look identical blurred or not -- an
    # obscuring effect needs real per-pixel variation to be provably
    # visible on screen. Mirrors test_shapes.py's own make_gradient_image,
    # just wrapped in a Frame for OverlayWindow's sake.
    width, height = size
    image = QImage(width, height, QImage.Format.Format_RGB32)
    for x in range(width):
        red = round(255 * x / (width - 1))
        for y in range(height):
            image.setPixelColor(x, y, QColor(red, 0, 0))
    return Frame(image=image, logical_origin=QPointF(0, 0), logical_size=QSizeF(*size))


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

        assert pixel(rendered, 70, 70) == base_color
        outside = pixel(rendered, 10, 10)
        assert outside != base_color
        assert outside.red() < base_color.red()

    def test_no_selection_dims_the_whole_monitor(self):
        frame = make_frame()
        overlay = Overlay(frame, QRectF(0, 0, 200, 200))

        rendered = overlay.grab().toImage()
        base_color = QColor(10, 20, 30)

        for x, y in [(10, 10), (100, 100), (190, 190)]:
            assert pixel(rendered, x, y) != base_color

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
        assert pixel(left_image, 170, 70) == base_color  # inside, on the left
        assert pixel(left_image, 10, 10) != base_color  # outside

        right_image = overlays[1].grab().toImage()
        assert pixel(right_image, 20, 100) == base_color  # inside, on the right
        assert pixel(right_image, 190, 190) != base_color  # outside


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
        sampled = pixel(rendered, round(center.x()), round(center.y()))

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
        sampled = pixel(rendered, round(sample_point.x()), round(sample_point.y()))

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
        sampled = pixel(rendered, round(center.x()), round(center.y()))

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

    def test_veil_is_painted_outside_the_path_not_the_bounding_box(self):
        # SNX-49 AC: the scrim inverts against the lasso itself, so a point
        # inside the L's own bounding box but in the notch it cuts away
        # (unlike test_confirms_bounds_and_excludes_pixels_outside_the_path's
        # plain path.contains() check) must still read as dimmed, not as
        # the undimmed base colour a bounding-box-only hole would show.
        frame = make_frame()
        overlay = Overlay(frame, QRectF(0, 0, 200, 200), mode=SelectionMode.FREEFORM)

        self._trace_l_shape(overlay)

        rendered = overlay.grab().toImage()
        base_color = QColor(10, 20, 30)
        assert pixel(rendered, 30, 30) == base_color  # inside the stem
        assert pixel(rendered, 80, 100) == base_color  # inside the foot
        assert pixel(rendered, 80, 40) != base_color  # in the notch: dimmed

    def test_veil_follows_the_path_live_mid_drag(self):
        frame = make_frame()
        overlay = Overlay(frame, QRectF(0, 0, 200, 200), mode=SelectionMode.FREEFORM)

        QTest.mousePress(overlay, Qt.MouseButton.LeftButton, pos=self._STEM_TOP_LEFT)
        QTest.mouseMove(overlay, self._STEM_TOP_RIGHT)
        QTest.mouseMove(overlay, self._NOTCH_CORNER)
        # Traced so far: (20,20) -> (60,20) -> (60,80), an open path whose
        # bounding box is (20,20)-(60,80). Qt implicitly closes an open
        # path for filling purposes (a straight line from (60,80) back to
        # (20,20)), so (25, 75) sits inside that bounding box but on the far
        # side of that implicit closing edge -- outside the filled shape.

        rendered = overlay.grab().toImage()
        base_color = QColor(10, 20, 30)
        assert pixel(rendered, 30, 30) == base_color  # inside the traced shape
        assert pixel(rendered, 25, 75) != base_color  # in the bbox, outside it: dimmed

        QTest.mouseRelease(overlay, Qt.MouseButton.LeftButton, pos=self._NOTCH_CORNER)

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

    @skip_on_windows(
        "hover-only QTest.mouseMove synthesis depends on the freshly-shown "
        "overlay being the OS-active window; Windows enforces real window "
        "activation even under the offscreen QPA platform, so a window left "
        "active by an earlier test in the same process (there is one shared "
        "QApplication per run) can swallow the synthetic move. X11/Wayland's "
        "offscreen backend does not enforce this, which is why it only holds "
        "on the target platform."
    )
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


class _FakeWindowsUser32:
    """Just enough of `ctypes.windll.user32` for one `EnumWindows` pass
    reporting a single, visible, non-minimised window -- see
    `TestWindowsWindowGeometryProviderIntegration` below. An ordinary
    (non-shell) class name and a monitor large enough to contain any test
    rect, so SNX-94's class-name/monitor-size checks never trip it.
    """

    def __init__(self, hwnd=1, title="Some Window"):
        self._hwnd = hwnd
        self._title = title

    def EnumWindows(self, callback, lparam):
        callback(self._hwnd, lparam)
        return 1

    def IsWindowVisible(self, hwnd):
        return 1

    def IsIconic(self, hwnd):
        return 0

    def GetWindowTextLengthW(self, hwnd):
        return len(self._title)

    def GetWindowTextW(self, hwnd, buffer, _size):
        buffer.value = self._title
        return len(self._title)

    def GetClassNameW(self, hwnd, buffer, _size):
        buffer.value = "SomeAppWindow"
        return len(buffer.value)

    def MonitorFromWindow(self, hwnd, _flags):
        return hwnd

    def GetMonitorInfoW(self, hmonitor, info_ref):
        target = ctypes.cast(info_ref, ctypes.POINTER(capture_module._MonitorInfo)).contents
        target.rcMonitor.left, target.rcMonitor.top = -1_000_000, -1_000_000
        target.rcMonitor.right, target.rcMonitor.bottom = 1_000_000, 1_000_000
        return 1


class _FakeWindowsDwmapi:
    """Reports one fixed extended-frame-bounds rect for every window, and
    "not cloaked" -- everything `TestWindowsWindowGeometryProviderIntegration`
    needs `DwmGetWindowAttribute` to answer.
    """

    def __init__(self, bounds):
        self._bounds = bounds  # (left, top, right, bottom)

    def DwmGetWindowAttribute(self, hwnd, attribute, out_ref, _size):
        if attribute == WindowsWindowGeometryProvider._DWMWA_CLOAKED:
            ctypes.cast(out_ref, ctypes.POINTER(ctypes.c_int)).contents.value = 0
            return 0
        target = ctypes.cast(out_ref, ctypes.POINTER(capture_module._RECT)).contents
        target.left, target.top, target.right, target.bottom = self._bounds
        return 0


class TestWindowsWindowGeometryProviderIntegration:
    """SNX-90's Windows counterpart to
    `TestX11WindowGeometryProviderIntegration` above: proves the real
    `WindowsWindowGeometryProvider` (not just `TestWindowMode`'s fake)
    satisfies `Overlay`'s expectations end to end -- a real `EnumWindows`/
    `DwmGetWindowAttribute` call, mocked at the ctypes boundary, feeding
    straight into window-mode click handling.
    """

    HIT_POINT = QPoint(50, 50)  # inside (30,30)-(80,80)

    def test_click_on_an_enumerated_window_confirms_its_extended_frame_bounds(
        self, monkeypatch
    ):
        monkeypatch.setattr("snipux.capture.sys.platform", "win32")
        monkeypatch.setattr(
            "snipux.capture.ctypes.windll",
            SimpleNamespace(
                user32=_FakeWindowsUser32(),
                dwmapi=_FakeWindowsDwmapi(bounds=(30, 30, 80, 80)),
            ),
            raising=False,
        )
        # ctypes.WINFUNCTYPE is Windows-only in the stdlib itself; CFUNCTYPE
        # builds an equally callable-from-Python function pointer and is
        # available everywhere, which is all the enum callback below needs
        # from it in a test that never crosses into real Win32 code.
        monkeypatch.setattr(
            "snipux.capture.ctypes.WINFUNCTYPE", ctypes.CFUNCTYPE, raising=False
        )
        provider = WindowsWindowGeometryProvider()

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
                assert pixel(rendered, x, y) == base_color


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

        assert pixel(rendered, 70, 70) == QColor(10, 20, 30)

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
            assert pixel(rendered, x, y) == QColor(10, 20, 30)

    def test_scrim_outside_selection_uses_the_dim_token_colour_and_alpha(self):
        frame = make_frame()
        overlay = OverlayWindow(frame)
        overlay.set_selection(QRect(50, 50, 50, 50))

        rendered = overlay.grab().toImage()
        expected = _blend(QColor(10, 20, 30), design_color("DIM"))
        sampled = pixel(rendered, 10, 10)

        # Small tolerance for Qt's own (premultiplied-alpha) rounding vs.
        # the plain-float blend computed in _blend above.
        assert sampled.red() == pytest.approx(expected.red(), abs=2)
        assert sampled.green() == pytest.approx(expected.green(), abs=2)
        assert sampled.blue() == pytest.approx(expected.blue(), abs=2)

    def test_freeform_scrim_inverts_against_the_path_not_the_bounding_box(self):
        # SNX-49 AC: an L-shaped path whose bounding box is (20,20)-(100,120)
        # -- the notch cut out of its top-right must stay dimmed even
        # though it sits squarely inside that bounding box.
        frame = make_frame()
        overlay = OverlayWindow(frame)
        path = QPainterPath()
        path.moveTo(QPointF(20, 20))
        path.lineTo(QPointF(60, 20))
        path.lineTo(QPointF(60, 80))
        path.lineTo(QPointF(100, 80))
        path.lineTo(QPointF(100, 120))
        path.lineTo(QPointF(20, 120))
        path.closeSubpath()
        overlay.set_selection(QRect(20, 20, 80, 100), path=path)

        rendered = overlay.grab().toImage()
        base_color = QColor(10, 20, 30)

        assert pixel(rendered, 30, 30) == base_color  # inside the stem
        assert pixel(rendered, 80, 100) == base_color  # inside the foot
        assert pixel(rendered, 80, 40) != base_color  # in the notch: dimmed

    def test_no_selection_dims_the_whole_window(self):
        frame = make_frame()
        overlay = OverlayWindow(frame)

        rendered = overlay.grab().toImage()
        base_color = QColor(10, 20, 30)

        for x, y in [(10, 10), (100, 100), (190, 190)]:
            assert pixel(rendered, x, y) != base_color

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

        assert pixel(rendered, 10, 10) == QColor(10, 20, 30)

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

        # Visible children only: chrome that is currently hidden (the
        # pre-snip chooser and its own labels, before it has been shown and
        # laid out) carries default geometry and paints nothing, so its
        # size says nothing about what covers the scrim.
        for child in overlay.findChildren(QWidget):
            if not child.isVisibleTo(overlay):
                continue
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

        assert pixel(rendered, 20, 50) == self.RED  # left border

    def test_mark_outside_the_selection_is_clipped_not_painted(self):
        frame = make_frame(image_size=(200, 200), logical_size=(200, 200))
        overlay = OverlayWindow(frame)
        overlay.set_selection(QRect(100, 100, 50, 50))
        overlay.add_mark(self._mark((10, 10), (30, 30)))

        rendered = overlay.grab().toImage()

        assert pixel(rendered, 20, 20) != self.RED

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

        assert pixel(rendered, 10, 20) == self.RED  # left border

    def test_reframing_leaves_a_mark_over_the_same_content(self):
        # A mark drawn inside the selection must stay over the same pixels
        # after the selection is re-framed -- the whole reason ink moved out
        # of selection-relative coordinates. Growing the selection (same
        # top-left) must not shift where the mark's own left border paints.
        frame = make_frame(image_size=(200, 200), logical_size=(200, 200))
        overlay = OverlayWindow(frame)
        overlay.set_selection(QRect(0, 0, 100, 100))
        overlay.add_mark(self._mark((20, 20), (40, 40)))

        before = pixel(overlay.grab().toImage(), 20, 30)

        overlay.set_selection(QRect(0, 0, 150, 150))
        after = pixel(overlay.grab().toImage(), 20, 30)

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
        assert pixel(result, 10, 20) == self.RED

    def test_rendered_image_contains_a_restored_shape_tools_mark(self):
        # SNX-64: same export path as the test above, but drawn through the
        # real press/move/release tool -- ellipse here -- rather than a
        # hand-built Shape, and reading the ellipse's own leftmost point
        # (vertically centred in its bounding box) rather than a
        # rectangle's flat left border. Press/release land at least 20px
        # inside every selection edge (unlike the (60, 60) corner the
        # Rectangle test above adds its mark at directly, bypassing mouse
        # events entirely) so the press isn't mistaken for a resize handle.
        frame = make_frame(image_size=(200, 200), logical_size=(200, 200))
        overlay = OverlayWindow(frame)
        overlay.set_selection(QRect(50, 50, 100, 100))
        overlay._bar.select_tool("ellipse")
        overlay._ink_colour = "#ff0000"
        overlay._stroke_width = 6

        QTest.mousePress(overlay, Qt.MouseButton.LeftButton, pos=QPoint(80, 80))
        QTest.mouseMove(overlay, QPoint(120, 120))
        QTest.mouseRelease(overlay, Qt.MouseButton.LeftButton, pos=QPoint(120, 120))

        result = overlay.rendered_image()

        # (80, 100) in window coordinates -- the ellipse's own leftmost
        # point, vertically centred -- is (30, 50) inside the crop.
        assert pixel(result, 30, 50) == self.RED


class TestOverlayWindowObscuringMarks:
    """SNX-63: a committed Blur/Pixelate mark used to be invisible until
    export -- `_paint_marks` skipped every `ObscuringShape` outright, with
    a "later ticket" comment nothing ever picked up. `_base_layer_image`
    now bakes every committed obscuring mark into Layer 1 itself, so the
    ink layer stops lying about what release does.
    """

    def test_committed_blur_shows_blurred_pixels_immediately_after_release(self):
        frame = make_gradient_frame()
        overlay = OverlayWindow(frame)
        overlay.set_selection(QRect(0, 0, 200, 200))
        overlay._bar.select_tool("blur")
        overlay._blur_mode = "blur"

        QTest.mousePress(overlay, Qt.MouseButton.LeftButton, pos=QPoint(20, 20))
        QTest.mouseMove(overlay, QPoint(120, 120))
        QTest.mouseRelease(overlay, Qt.MouseButton.LeftButton, pos=QPoint(120, 120))

        # No export call anywhere in this test -- this is what release
        # itself paints, deep inside the mark's own rect so a marquee
        # outline (the in-progress preview's own look) could never account
        # for the difference.
        raw = frame.image.pixelColor(70, 70)
        rendered = pixel(overlay.grab().toImage(), 70, 70)
        assert rendered != raw

    def test_committed_pixelate_shows_its_blocks_on_screen(self):
        # Same probe technique test_shapes.py's TestPixelateBlocky uses:
        # a coarser (higher-strength) downsample spans a wider block, so
        # two probes that straddle the fine block's edge still read equal
        # once pixelated -- proof this is genuinely blocky, not just
        # blurred. The mark's own rect is inset from the selection's edges
        # (kept at the window's own full size) so the probes below don't
        # land on the selection frame's corner brackets/edge handles,
        # which are painted after -- and on top of -- the ink layer.
        patch = 80
        offset = 20
        strength = tokens.Metric.BLUR_DEFAULT
        frame = make_gradient_frame(size=(200, 200))
        overlay = OverlayWindow(frame)
        overlay.set_selection(QRect(0, 0, 200, 200))
        overlay.add_mark(
            Pixelate(
                colour=QColor("#ff0000"),
                stroke_width=4,
                start=QPointF(offset, offset),
                end=QPointF(offset + patch, offset + patch),
                strength=strength,
            )
        )

        rendered = overlay.grab().toImage()

        block_width = patch // (patch // strength)
        row = offset + 40
        probe_a, probe_b = offset + 1, offset + block_width + 1
        assert pixel(rendered, probe_a, row) == pixel(rendered, offset + 3, row)
        assert pixel(rendered, probe_a, row) != pixel(rendered, probe_b, row)

    def test_on_screen_result_matches_the_exported_image_for_the_same_mark(self):
        frame = make_gradient_frame()
        overlay = OverlayWindow(frame)
        overlay.set_selection(QRect(0, 0, 200, 200))
        overlay.add_mark(
            Blur(colour=QColor("#ff0000"), stroke_width=4, start=QPointF(20, 20), end=QPointF(120, 120))
        )

        on_screen = overlay.grab().toImage()
        exported = overlay.rendered_image()

        # The selection spans the whole window at its own (0, 0) origin,
        # so window coordinates and the exported crop's coordinates are
        # the same pixels here -- a direct probe comparison is valid.
        assert pixel(on_screen, 70, 70) == pixel(exported, 70, 70)

    def test_changing_strength_on_an_already_committed_mark_updates_its_look(self):
        # Inset from the selection's own edges for the same reason as
        # test_committed_pixelate_shows_its_blocks_on_screen above -- kept
        # clear of the corner brackets/edge handles painted over the ink
        # layer.
        patch = 80
        offset = 20
        frame = make_gradient_frame(size=(200, 200))
        overlay = OverlayWindow(frame)
        overlay.set_selection(QRect(0, 0, 200, 200))
        overlay.add_mark(
            Pixelate(
                colour=QColor("#ff0000"),
                stroke_width=4,
                start=QPointF(offset, offset),
                end=QPointF(offset + patch, offset + patch),
                strength=tokens.Metric.BLUR_MIN,
            )
        )
        mark = overlay.marks[0]
        low_block_width = patch // (patch // tokens.Metric.BLUR_MIN)
        row = offset + 40
        probe_a, probe_b = offset + 1, offset + low_block_width + 1

        low_strength = overlay.grab().toImage()
        # A pixel just past the low-strength block boundary already read
        # differently from the block before it...
        assert pixel(low_strength, probe_a, row) != pixel(low_strength, probe_b, row)

        # ...simulates the settings tray's strength slider retuning the
        # mark just drawn, still committed, still the same object.
        mark.strength = tokens.Metric.BLUR_MAX

        high_strength = overlay.grab().toImage()
        # The coarser block now spans both probes, so they read equal --
        # proof the already-committed mark's look actually changed.
        assert pixel(high_strength, probe_a, row) == pixel(high_strength, probe_b, row)

    def test_reframing_keeps_an_obscuring_mark_aligned_to_its_own_pixels(self):
        # Same property TestOverlayWindowMarks's own
        # test_reframing_leaves_a_mark_over_the_same_content pins for an
        # ordinary painted mark: a re-frame must never move where a
        # committed mark's own effect sits, obscuring marks included.
        frame = make_gradient_frame()
        overlay = OverlayWindow(frame)
        overlay.set_selection(QRect(0, 0, 100, 100))
        overlay.add_mark(
            Blur(colour=QColor("#ff0000"), stroke_width=4, start=QPointF(20, 20), end=QPointF(60, 60))
        )

        before = pixel(overlay.grab().toImage(), 40, 40)

        overlay.set_selection(QRect(0, 0, 150, 150))
        after = pixel(overlay.grab().toImage(), 40, 40)

        assert before == after

    def test_repeated_repaints_do_not_recompute_obscuring_marks_each_time(self, monkeypatch):
        # Per this ticket's own performance acceptance criterion: a
        # repaint triggered by something unrelated -- an in-progress pen
        # stroke elsewhere on the canvas -- must not redo blur/pixelate's
        # scale-down/scale-up sampling on every single frame once nothing
        # about the obscuring marks themselves has changed.
        frame = make_gradient_frame()
        overlay = OverlayWindow(frame)
        overlay.set_selection(QRect(0, 0, 200, 200))
        overlay.add_mark(
            Blur(colour=QColor("#ff0000"), stroke_width=4, start=QPointF(10, 10), end=QPointF(60, 60))
        )
        overlay.add_mark(
            Pixelate(
                colour=QColor("#ff0000"), stroke_width=4, start=QPointF(80, 80), end=QPointF(130, 130)
            )
        )

        calls = []
        original_apply = ObscuringShape.apply

        def counting_apply(self, image):
            calls.append(self)
            return original_apply(self, image)

        monkeypatch.setattr(ObscuringShape, "apply", counting_apply)

        overlay.grab()  # first paint under the patch: bakes both marks once
        assert len(calls) == 2

        overlay._in_progress_shape = Pen(
            colour=QColor("#00ff00"), stroke_width=3, points=[QPointF(150, 150)]
        )
        for x in range(150, 160):
            overlay._in_progress_shape.points.append(QPointF(x, 150))
            overlay.grab()

        assert len(calls) == 2  # unchanged: the cached bake was reused every time


class TestEraserTool:
    """SNX-38: per-shape hit-testing itself lives on `Shape` (shapes.py --
    see TestShapeHitTest in test_shapes.py); this class covers how
    OverlayWindow wires that into a click. SNX-70 folds erase_at's own
    undo into the general undo/redo stack (TestUndoRedoClear below covers
    that fold in full); the erase_at-specific mechanics -- which mark a
    click removes, a miss being a safe no-op -- stay here.
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

    def test_erase_at_leaves_undo_available(self):
        # SNX-70: erasing used to land in its own private slot
        # (`undo_erase`) that neither Ctrl+Z nor the bar's Undo button ever
        # reached -- can_undo staying False right after an erase was the
        # bug. See TestUndoRedoClear for the full undo/redo round trip.
        overlay = self._overlay()
        overlay.add_mark(self._mark((20, 20), (80, 80)))

        overlay.erase_at(QPointF(20, 50))

        assert overlay.can_undo

    @pytest.mark.parametrize(
        "mark, hit_point",
        [
            # Ellipse/Crop: the bounding box's own left border, vertically
            # centred -- same probe Rectangle's own hit-testing already
            # relies on elsewhere in this file.
            (
                Ellipse(colour=RED, stroke_width=6, start=QPointF(20, 20), end=QPointF(80, 80)),
                QPoint(20, 50),
            ),
            # Line has no left border to speak of -- its own diagonal is
            # the only place a click can land on it.
            (
                Line(colour=RED, stroke_width=6, start=QPointF(20, 20), end=QPointF(80, 80)),
                QPoint(50, 50),
            ),
            (
                Crop(colour=RED, stroke_width=6, start=QPointF(20, 20), end=QPointF(80, 80)),
                QPoint(20, 50),
            ),
        ],
    )
    def test_click_with_eraser_active_removes_a_restored_tools_mark(self, mark, hit_point):
        # SNX-64: Crop had no hit_test override at all before this ticket --
        # unlike Ellipse/Line, which already had one -- so this is what
        # proves the eraser can reach all three restored tools' marks, not
        # just the two shapes.py already covered.
        overlay = self._overlay()
        overlay.add_mark(mark)
        overlay.set_eraser_active(True)

        QTest.mousePress(overlay, Qt.MouseButton.LeftButton, pos=hit_point)

        assert overlay.marks == ()

    def test_cursor_is_a_pointer_over_the_selection_while_the_eraser_is_active(self):
        overlay = self._overlay(selection=QRect(50, 50, 100, 80))
        overlay.set_eraser_active(True)
        overlay.show()
        QTest.qWaitForWindowExposed(overlay)

        QTest.mouseMove(overlay, QPoint(100, 90))  # deep inside the selection

        assert overlay.cursor().shape() == Qt.CursorShape.PointingHandCursor


class TestDrawingTools:
    """SNX-52: a press inside the selection starts a mark for whichever
    tool `_bar.active_tool` names; move extends it; release either commits
    it (via shapes.finalize_mark) or discards it if it never reached the
    spec's minimum size. A committed mark takes its colour/stroke from
    `_ink_colour`/`_stroke_width`, and -- for blur -- its shape class and
    strength from `_blur_mode`/`_blur_strength`, per
    docs/design/overlay-redesign.md's "Drawing".
    """

    def _overlay(self, selection=QRect(0, 0, 200, 200)):
        frame = make_frame(image_size=(200, 200), logical_size=(200, 200))
        overlay = OverlayWindow(frame)
        overlay.set_selection(selection)
        return overlay

    def test_pen_press_move_release_commits_a_polyline(self):
        overlay = self._overlay()
        overlay._bar.select_tool("pen")

        QTest.mousePress(overlay, Qt.MouseButton.LeftButton, pos=QPoint(20, 20))
        QTest.mouseMove(overlay, QPoint(40, 40))
        QTest.mouseMove(overlay, QPoint(60, 30))
        QTest.mouseRelease(overlay, Qt.MouseButton.LeftButton, pos=QPoint(60, 30))

        assert len(overlay.marks) == 1
        mark = overlay.marks[0]
        assert isinstance(mark, Pen)
        assert mark.points == [QPointF(20, 20), QPointF(40, 40), QPointF(60, 30)]

    def test_highlighter_press_move_release_commits_a_polyline(self):
        overlay = self._overlay()
        overlay._bar.select_tool("highlighter")

        QTest.mousePress(overlay, Qt.MouseButton.LeftButton, pos=QPoint(20, 20))
        QTest.mouseMove(overlay, QPoint(50, 50))
        QTest.mouseRelease(overlay, Qt.MouseButton.LeftButton, pos=QPoint(50, 50))

        assert len(overlay.marks) == 1
        assert isinstance(overlay.marks[0], Highlighter)

    def test_pen_stroke_with_only_a_press_and_release_is_discarded(self):
        # No mouseMoveEvent in between: the stroke never grows past its one
        # anchor point, below finalize_mark's freehand minimum.
        overlay = self._overlay()
        overlay._bar.select_tool("pen")

        QTest.mousePress(overlay, Qt.MouseButton.LeftButton, pos=QPoint(20, 20))
        QTest.mouseRelease(overlay, Qt.MouseButton.LeftButton, pos=QPoint(20, 20))

        assert overlay.marks == ()

    def test_arrow_press_move_release_commits_from_press_to_release(self):
        overlay = self._overlay()
        overlay._bar.select_tool("arrow")

        QTest.mousePress(overlay, Qt.MouseButton.LeftButton, pos=QPoint(20, 20))
        QTest.mouseMove(overlay, QPoint(80, 60))
        QTest.mouseRelease(overlay, Qt.MouseButton.LeftButton, pos=QPoint(80, 60))

        assert len(overlay.marks) == 1
        mark = overlay.marks[0]
        assert isinstance(mark, Arrow)
        assert mark.start == QPointF(20, 20)
        assert mark.end == QPointF(80, 60)

    def test_rect_press_move_release_commits_from_press_to_release(self):
        overlay = self._overlay()
        overlay._bar.select_tool("rect")

        QTest.mousePress(overlay, Qt.MouseButton.LeftButton, pos=QPoint(20, 20))
        QTest.mouseMove(overlay, QPoint(80, 60))
        QTest.mouseRelease(overlay, Qt.MouseButton.LeftButton, pos=QPoint(80, 60))

        assert len(overlay.marks) == 1
        mark = overlay.marks[0]
        assert isinstance(mark, Rectangle)
        assert mark.start == QPointF(20, 20)
        assert mark.end == QPointF(80, 60)

    def test_in_progress_rect_is_visible_mid_drag(self):
        # Same stroke width/colour TestOverlayWindowMarks's own painted-mark
        # tests already rely on for a clean, fully-covered sample pixel.
        overlay = self._overlay()
        overlay._bar.select_tool("rect")
        overlay._ink_colour = "#ff0000"
        overlay._stroke_width = 6

        QTest.mousePress(overlay, Qt.MouseButton.LeftButton, pos=QPoint(20, 20))
        QTest.mouseMove(overlay, QPoint(80, 60))

        rendered = overlay.grab().toImage()
        assert pixel(rendered, 20, 40) == QColor("#ff0000")  # left edge of the live preview

        QTest.mouseRelease(overlay, Qt.MouseButton.LeftButton, pos=QPoint(80, 60))

    def test_ellipse_press_move_release_commits_from_press_to_release(self):
        # SNX-64: restored via `_TWO_POINT_MARK_CLASSES`, reached through
        # rect's own shape submenu -- see TestShapeToolPopover -- but the
        # commit itself goes through the exact same press/move/release path
        # rect and arrow already use.
        overlay = self._overlay()
        overlay._bar.select_tool("ellipse")

        QTest.mousePress(overlay, Qt.MouseButton.LeftButton, pos=QPoint(20, 20))
        QTest.mouseMove(overlay, QPoint(80, 60))
        QTest.mouseRelease(overlay, Qt.MouseButton.LeftButton, pos=QPoint(80, 60))

        assert len(overlay.marks) == 1
        mark = overlay.marks[0]
        assert isinstance(mark, Ellipse)
        assert mark.start == QPointF(20, 20)
        assert mark.end == QPointF(80, 60)

    def test_line_press_move_release_commits_from_press_to_release(self):
        overlay = self._overlay()
        overlay._bar.select_tool("line")

        QTest.mousePress(overlay, Qt.MouseButton.LeftButton, pos=QPoint(20, 20))
        QTest.mouseMove(overlay, QPoint(80, 60))
        QTest.mouseRelease(overlay, Qt.MouseButton.LeftButton, pos=QPoint(80, 60))

        assert len(overlay.marks) == 1
        mark = overlay.marks[0]
        assert isinstance(mark, Line)
        assert mark.start == QPointF(20, 20)
        assert mark.end == QPointF(80, 60)

    def test_crop_press_move_release_commits_from_press_to_release(self):
        overlay = self._overlay()
        overlay._bar.select_tool("crop")

        QTest.mousePress(overlay, Qt.MouseButton.LeftButton, pos=QPoint(20, 20))
        QTest.mouseMove(overlay, QPoint(80, 60))
        QTest.mouseRelease(overlay, Qt.MouseButton.LeftButton, pos=QPoint(80, 60))

        assert len(overlay.marks) == 1
        mark = overlay.marks[0]
        assert isinstance(mark, Crop)
        assert mark.start == QPointF(20, 20)
        assert mark.end == QPointF(80, 60)

    @pytest.mark.parametrize("tool", ["ellipse", "line", "crop"])
    def test_restored_shape_tools_take_ink_colour_and_stroke_from_the_tray(self, tool):
        overlay = self._overlay()
        overlay._bar.select_tool(tool)
        overlay._ink_colour = "#38bdf8"
        overlay._stroke_width = 9

        QTest.mousePress(overlay, Qt.MouseButton.LeftButton, pos=QPoint(20, 20))
        QTest.mouseMove(overlay, QPoint(80, 60))
        QTest.mouseRelease(overlay, Qt.MouseButton.LeftButton, pos=QPoint(80, 60))

        mark = overlay.marks[0]
        assert mark.colour == QColor("#38bdf8")
        assert mark.stroke_width == 9

    def test_blur_press_move_release_commits_a_blur_shape(self):
        overlay = self._overlay()
        overlay._bar.select_tool("blur")
        overlay._blur_mode = "blur"
        overlay._blur_strength = 12

        QTest.mousePress(overlay, Qt.MouseButton.LeftButton, pos=QPoint(20, 20))
        QTest.mouseMove(overlay, QPoint(80, 60))
        QTest.mouseRelease(overlay, Qt.MouseButton.LeftButton, pos=QPoint(80, 60))

        assert len(overlay.marks) == 1
        mark = overlay.marks[0]
        assert isinstance(mark, Blur)
        assert not isinstance(mark, Pixelate)
        assert mark.start == QPointF(20, 20)
        assert mark.end == QPointF(80, 60)
        assert mark.strength == 12

    def test_pixelate_mode_commits_a_pixelate_shape(self):
        overlay = self._overlay()
        overlay._bar.select_tool("blur")
        overlay._blur_mode = "pix"

        QTest.mousePress(overlay, Qt.MouseButton.LeftButton, pos=QPoint(20, 20))
        QTest.mouseMove(overlay, QPoint(80, 60))
        QTest.mouseRelease(overlay, Qt.MouseButton.LeftButton, pos=QPoint(80, 60))

        assert isinstance(overlay.marks[0], Pixelate)

    def test_tiny_blur_drag_is_discarded_on_release(self):
        overlay = self._overlay()
        overlay._bar.select_tool("blur")

        QTest.mousePress(overlay, Qt.MouseButton.LeftButton, pos=QPoint(20, 20))
        QTest.mouseMove(overlay, QPoint(21, 21))
        QTest.mouseRelease(overlay, Qt.MouseButton.LeftButton, pos=QPoint(21, 21))

        assert overlay.marks == ()

    def test_step_commits_on_a_click_alone_with_no_drag(self):
        overlay = self._overlay()
        overlay._bar.select_tool("step")

        QTest.mousePress(overlay, Qt.MouseButton.LeftButton, pos=QPoint(30, 30))

        # Committed on the press itself -- never arms a drag to release.
        assert overlay._in_progress_shape is None
        assert len(overlay.marks) == 1
        mark = overlay.marks[0]
        assert isinstance(mark, StepMarker)
        assert mark.point == QPointF(30, 30)
        assert mark.number == 1

        QTest.mouseRelease(overlay, Qt.MouseButton.LeftButton, pos=QPoint(30, 30))

        assert len(overlay.marks) == 1  # release adds nothing further

    def test_text_click_opens_the_label_editor_with_no_mark_yet(self):
        overlay = self._overlay()
        overlay.show()
        QTest.qWaitForWindowExposed(overlay)
        overlay._bar.select_tool("text")

        QTest.mousePress(overlay, Qt.MouseButton.LeftButton, pos=QPoint(30, 30))
        QApplication.processEvents()

        assert overlay.marks == ()
        assert overlay._text_edit is not None
        assert not overlay._text_edit.isHidden()
        assert overlay._text_edit.placeholderText() == "Label"

        QTest.mouseRelease(overlay, Qt.MouseButton.LeftButton, pos=QPoint(30, 30))

    def test_text_commits_once_typed_and_editing_finishes(self):
        overlay = self._overlay()
        overlay.show()
        QTest.qWaitForWindowExposed(overlay)
        overlay._bar.select_tool("text")

        QTest.mousePress(overlay, Qt.MouseButton.LeftButton, pos=QPoint(30, 30))
        QApplication.processEvents()
        QTest.keyClicks(overlay._text_edit, "hello")
        QTest.keyClick(overlay._text_edit, Qt.Key.Key_Return)

        assert len(overlay.marks) == 1
        mark = overlay.marks[0]
        assert isinstance(mark, Text)
        assert mark.text == "hello"
        assert mark.point == QPointF(30, 30)
        assert overlay._text_edit.isHidden()

    def test_text_editing_finished_with_nothing_typed_commits_no_mark(self):
        overlay = self._overlay()
        overlay.show()
        QTest.qWaitForWindowExposed(overlay)
        overlay._bar.select_tool("text")

        QTest.mousePress(overlay, Qt.MouseButton.LeftButton, pos=QPoint(30, 30))
        QApplication.processEvents()
        QTest.keyClick(overlay._text_edit, Qt.Key.Key_Return)

        assert overlay.marks == ()

    def test_second_label_click_commits_the_first_instead_of_discarding_it(self):
        # SNX-77: clicking to place a second label used to clear() the
        # shared QLineEdit before editingFinished ever got a chance to
        # commit the first one -- nothing forced focus away from the field
        # on this path (unlike a toolbar click), so the first label's text
        # was simply wiped. Placing several labels in a row is the ordinary
        # way to annotate a screenshot, so both must survive.
        overlay = self._overlay()
        overlay.show()
        QTest.qWaitForWindowExposed(overlay)
        overlay._bar.select_tool("text")
        overlay._ink_colour = "#123456"
        overlay._stroke_width = 5

        QTest.mousePress(overlay, Qt.MouseButton.LeftButton, pos=QPoint(30, 30))
        QApplication.processEvents()
        QTest.keyClicks(overlay._text_edit, "first")

        # Changed before the second click, so the assertions below can tell
        # apart "the first label kept its own colour/size" from "it silently
        # picked up whatever the tray happens to hold at commit time."
        overlay._ink_colour = "#abcdef"
        overlay._stroke_width = 12

        QTest.mousePress(overlay, Qt.MouseButton.LeftButton, pos=QPoint(80, 80))
        QApplication.processEvents()
        QTest.keyClicks(overlay._text_edit, "second")
        QTest.keyClick(overlay._text_edit, Qt.Key.Key_Return)

        assert len(overlay.marks) == 2
        first, second = overlay.marks
        assert isinstance(first, Text)
        assert isinstance(second, Text)
        assert first.text == "first"
        assert second.text == "second"
        # The first label keeps the position and styling it was typed at,
        # not the second click's.
        assert first.point == QPointF(30, 30)
        assert first.colour == QColor("#123456")
        assert first.stroke_width == 5
        assert second.point == QPointF(80, 80)
        assert second.colour == QColor("#abcdef")
        assert second.stroke_width == 12

    def test_text_label_focus_also_suppresses_shortcuts(self):
        # The other half of SNX-47's suppression AC (see
        # TestKeyboardShortcutSuppression), now against the real text-tool
        # editor this ticket wires up rather than a bare stand-in QLineEdit.
        overlay = self._overlay()
        overlay.show()
        QTest.qWaitForWindowExposed(overlay)
        overlay._bar.select_tool("text")
        QTest.mousePress(overlay, Qt.MouseButton.LeftButton, pos=QPoint(30, 30))
        QApplication.processEvents()

        QTest.keyClick(overlay, Qt.Key.Key_P)

        assert overlay._bar.active_tool == "text"  # the "P" shortcut never fired

        QTest.mouseRelease(overlay, Qt.MouseButton.LeftButton, pos=QPoint(30, 30))

    def test_press_on_a_handle_resizes_and_commits_no_mark(self):
        overlay = self._overlay(selection=QRect(0, 0, 100, 100))
        overlay._bar.select_tool("pen")
        handle_pos = overlay._edge_handle_rect(Handle.RIGHT).center().toPoint()

        QTest.mousePress(overlay, Qt.MouseButton.LeftButton, pos=handle_pos)
        QTest.mouseMove(overlay, handle_pos + QPoint(30, 0))
        QTest.mouseRelease(
            overlay, Qt.MouseButton.LeftButton, pos=handle_pos + QPoint(30, 0)
        )

        assert overlay.marks == ()
        assert overlay._selection.width() != 100  # the resize itself did happen

    def test_committed_mark_takes_the_current_tray_colour_and_stroke(self):
        overlay = self._overlay()
        overlay._bar.select_tool("pen")
        overlay._ink_colour = "#123456"
        overlay._stroke_width = 17

        QTest.mousePress(overlay, Qt.MouseButton.LeftButton, pos=QPoint(20, 20))
        QTest.mouseMove(overlay, QPoint(40, 40))
        QTest.mouseRelease(overlay, Qt.MouseButton.LeftButton, pos=QPoint(40, 40))

        mark = overlay.marks[0]
        assert mark.colour == QColor("#123456")
        assert mark.stroke_width == 17


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
            pixel(rendered, x, 50).getRgb()[:3]
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
            if pixel(rendered, self.SEL.left() + 15, y) == QColor(255, 255, 255):
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
            sampled = pixel(rendered, round(center.x()), round(center.y()))
            assert sampled == QColor(255, 255, 255), handle


@skip_on_windows(
    "cursor-shape assertions depend on QTest.mouseMove synthesizing a hover "
    "onto the freshly-shown overlay as the OS-active window; Windows enforces "
    "real window activation even under the offscreen QPA platform, so a "
    "window left active by an earlier test in the same process (one shared "
    "QApplication per run) steals it. X11/Wayland's offscreen backend does "
    "not enforce this, which is why it only holds on the target platform."
)
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

        # Outside the selection *and* clear of every other chrome widget --
        # (10, 10) used to qualify, but SNX-46's HintHUD spans the window's
        # full width for its own HUD_H=44px strip whenever hints are on
        # (SNX-65 turned that off by default, but this must hold for either
        # state), so a move there could land on that child widget instead of
        # reaching this one's own mouseMoveEvent at all (Qt delivers it to
        # whichever widget is actually under the point), leaving this cursor
        # stuck rather than unset. (280, 280) sits below the HUD, below the
        # floating bar, and outside the selection, so it actually exercises
        # this widget's own cursor-reset branch regardless of the HUD's
        # visibility.
        QTest.mouseMove(overlay, QPoint(280, 280))
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

    def test_drag_keeps_the_selection_clear_of_the_left_edge(self):
        overlay = self._overlay()
        overlay.set_selection(QRect(100, 100, 150, 100))
        press = overlay._corner_hit_rect(Handle.TOP_LEFT).center().toPoint()

        self._drag(overlay, press, QPoint(-50, -50))

        sel = overlay._selection
        assert sel.x() == 0  # x >= 0
        # y >= 52 (clear of the hint HUD) only applies while hints are on;
        # this overlay's default is now off (SNX-65), so the top edge is
        # free to reach 0 -- see TestReframingClearsTheShownHUD for the
        # hints_enabled=True case that still clamps to _TOP_CLEARANCE.
        assert sel.y() == 0

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


class TestRegionDragToCreate:
    """SNX-57: Region -- the default mode, and the only one with no picking
    flag of its own -- gets the same "drag on an empty overlay" starting
    point Window/Full screen/Freeform each already have. Before this,
    `mousePressEvent` treated a press with no selection yet as a no-op for
    every tool, per its own comment, so Region -- the mode the whole tool
    is for -- had no way to ever produce a first selection at all.
    """

    def _overlay(self, size=(400, 400)):
        frame = make_frame(image_size=size, logical_size=size)
        return OverlayWindow(frame)

    def _drag(self, overlay, press_pos, move_pos):
        QTest.mousePress(overlay, Qt.MouseButton.LeftButton, pos=press_pos)
        QTest.mouseMove(overlay, move_pos)
        QTest.mouseRelease(overlay, Qt.MouseButton.LeftButton, pos=move_pos)

    def test_drag_on_a_fresh_overlay_creates_a_selection_that_follows_the_cursor(self):
        overlay = self._overlay()
        assert overlay._selection is None

        QTest.mousePress(overlay, Qt.MouseButton.LeftButton, pos=QPoint(50, 50))
        QTest.mouseMove(overlay, QPoint(150, 120))

        # Still mid-drag, not yet released, but already visible and
        # tracking the cursor, per "follows the cursor... during the drag."
        assert overlay._selection == QRect(50, 50, 100, 70)

        QTest.mouseRelease(overlay, Qt.MouseButton.LeftButton, pos=QPoint(150, 120))

        assert overlay._selection == QRect(50, 50, 100, 70)

    def test_release_commits_the_selection_and_shows_bar_chips_and_handles(self):
        overlay = self._overlay()
        overlay.show()
        QTest.qWaitForWindowExposed(overlay)

        self._drag(overlay, QPoint(50, 50), QPoint(150, 120))

        assert overlay._selection == QRect(50, 50, 100, 70)
        assert overlay._bar.isVisible()
        # The dimension chip reads live off `_selection` -- confirming it
        # reports this selection's own size is what "chips appear" reduces
        # to, since the chip itself is painted, not a separately-gated
        # child widget.
        size_text, _marks_text = overlay._dimension_chip_texts()
        assert size_text == "100 × 70"
        # A handle now hit-tests against the freshly-created selection, the
        # same corner-bracket geometry every other mode's selection gets.
        corner = overlay._corner_hit_rect(Handle.BOTTOM_RIGHT).center()
        assert overlay._handle_at(corner) is Handle.BOTTOM_RIGHT

    def test_selection_created_by_a_drag_can_then_be_reframed_by_its_handles(self):
        overlay = self._overlay()
        overlay.set_selection(QRect(100, 100, 150, 100))
        press = overlay._corner_hit_rect(Handle.TOP_LEFT).center().toPoint()

        self._drag(overlay, press, QPoint(60, 60))

        sel = overlay._selection
        # Bottom-right corner (the anchor for a top-left drag) is untouched
        # -- exactly `TestReframing`'s own re-framing assertions, run here
        # against a selection this ticket's own drag produced rather than
        # one seeded directly via `set_selection`.
        assert (sel.x(), sel.y()) == (60, 60)
        assert (sel.width(), sel.height()) == (190, 140)

    def test_drag_below_the_minimum_size_leaves_no_selection(self):
        overlay = self._overlay()

        # 5x4: under both tokens.Metric.SEL_MIN_W and SEL_MIN_H (16x16).
        self._drag(overlay, QPoint(50, 50), QPoint(55, 54))

        assert overlay._selection is None

    def test_plain_click_with_no_movement_leaves_no_selection(self):
        overlay = self._overlay()

        QTest.mouseClick(overlay, Qt.MouseButton.LeftButton, pos=QPoint(50, 50))

        assert overlay._selection is None

    def test_press_on_an_existing_selections_handle_resizes_it_not_a_new_drag(self):
        overlay = self._overlay()
        overlay.set_selection(QRect(50, 50, 100, 80))
        press = overlay._corner_hit_rect(Handle.TOP_LEFT).center().toPoint()

        QTest.mousePress(overlay, Qt.MouseButton.LeftButton, pos=press)

        # A handle hit is a resize, never a Region drag-to-create -- the
        # press-time state proves which of the two `mousePressEvent` chose.
        assert overlay._active_handle is Handle.TOP_LEFT
        assert overlay._region_drag_anchor is None

        QTest.mouseRelease(overlay, Qt.MouseButton.LeftButton, pos=press)

        # The original selection survives (aside from the resize itself),
        # rather than being replaced by a fresh drag-created rect.
        assert overlay._selection is not None


class TestUndoRedoClear:
    """SNX-39: the general undo/redo/clear stack over `_marks`. SNX-70 folds
    the eraser (TestEraserTool above) into this same stack -- `erase_at` no
    longer has a private single-slot undo of its own -- so an erase takes
    its turn in draw order alongside ordinary marks and rides the same
    Ctrl+Z / bar Undo button / redo / commit-clears-redo rules the tests
    below already cover for `add_mark`. SNX-72 folds `clear()` in the same
    way: a clear used to drop `_marks` and both stacks outright with no way
    back, and now takes its own turn in the same history instead.
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

    def test_undo_and_redo_cover_a_restored_shape_tools_mark(self):
        # SNX-64: undo/redo have no per-shape-type dispatch of their own
        # (see this class's own docstring) -- proving it works for whatever
        # `add_mark` was actually handed, drawn through the real tool
        # rather than a hand-built shapes.Ellipse, is what closes the gap
        # the acceptance criterion asks for.
        overlay = self._overlay()
        overlay._bar.select_tool("ellipse")
        QTest.mousePress(overlay, Qt.MouseButton.LeftButton, pos=QPoint(20, 20))
        QTest.mouseMove(overlay, QPoint(80, 60))
        QTest.mouseRelease(overlay, Qt.MouseButton.LeftButton, pos=QPoint(80, 60))
        mark = overlay.marks[0]

        overlay.undo()

        assert overlay.marks == ()
        assert overlay.can_redo

        overlay.redo()

        assert overlay.marks == (mark,)

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

    def test_clear_moves_every_mark_to_the_undo_stack(self):
        # SNX-72 AC: "the Undo button is enabled after a clear rather than
        # greyed out."
        overlay = self._overlay()
        overlay.add_mark(self._mark((0, 0), (10, 10)))
        overlay.add_mark(self._mark((20, 20), (30, 30)))

        overlay.clear()

        assert overlay.marks == ()
        assert overlay.can_undo

    def test_clear_still_empties_a_pre_existing_redo_stack(self):
        overlay = self._overlay()
        overlay.add_mark(self._mark((0, 0), (10, 10)))
        overlay.add_mark(self._mark((20, 20), (30, 30)))
        overlay.undo()  # one mark now sits on the redo stack
        assert overlay.can_redo

        overlay.clear()

        assert not overlay.can_redo

    def test_undo_after_clear_restores_every_mark_in_original_order(self):
        # SNX-72 AC: "clearing all annotations can be undone, restoring
        # every mark in its original draw order."
        overlay = self._overlay()
        first = self._mark((0, 0), (10, 10))
        second = self._mark((20, 20), (30, 30))
        overlay.add_mark(first)
        overlay.add_mark(second)

        overlay.clear()
        overlay.undo()

        assert overlay.marks == (first, second)

    def test_redo_re_clears(self):
        # SNX-72 AC: "redo re-clears, so the action round-trips like any
        # other action."
        overlay = self._overlay()
        overlay.add_mark(self._mark((0, 0), (10, 10)))
        overlay.add_mark(self._mark((20, 20), (30, 30)))
        overlay.clear()
        overlay.undo()
        assert overlay.marks != ()

        overlay.redo()

        assert overlay.marks == ()
        assert overlay.can_undo
        assert not overlay.can_redo

    def test_committing_a_mark_after_undoing_a_clear_empties_the_redo_stack(self):
        # SNX-72 AC: "committing a new mark after undoing a clear clears the
        # redo stack, the same rule every other action follows."
        overlay = self._overlay()
        overlay.add_mark(self._mark((0, 0), (10, 10)))
        overlay.clear()
        overlay.undo()
        assert overlay.can_redo  # the clear sitting on the redo stack

        overlay.add_mark(self._mark((50, 50), (60, 60)))

        assert not overlay.can_redo

    def test_clear_with_nothing_to_clear_is_a_no_op(self):
        # SNX-72 AC: "clearing when there is nothing to clear is still a
        # no-op and does not push an empty step onto the stack."
        overlay = self._overlay()

        overlay.clear()

        assert overlay.marks == ()
        assert not overlay.can_undo
        assert not overlay.can_redo

    def test_undo_restores_an_erased_mark_to_its_original_position(self):
        # SNX-70 AC: "erasing a mark leaves undo available, and Ctrl+Z
        # restores it to its original position in draw order."
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

        overlay.undo()

        # Restored between `first` and `third`, its original draw-order
        # position -- not appended to the end.
        assert overlay.marks == (first, second, third)

    def test_redo_erases_the_mark_again(self):
        # SNX-70 AC: "redo removes it again, so an erase round-trips like
        # any other action."
        overlay = self._overlay()
        mark = self._mark((20, 20), (80, 80))
        overlay.add_mark(mark)
        overlay.erase_at(QPointF(20, 50))
        overlay.undo()
        assert overlay.marks == (mark,)

        overlay.redo()

        assert overlay.marks == ()

    def test_undo_twice_after_draw_then_erase_leaves_neither(self):
        # SNX-70 AC: "an erase takes its turn in order alongside draws, so
        # undoing twice after draw-then-erase leaves neither."
        overlay = self._overlay()
        mark = self._mark((20, 20), (80, 80))
        overlay.add_mark(mark)
        overlay.erase_at(QPointF(20, 50))
        assert overlay.marks == ()

        overlay.undo()  # undoes the erase: mark comes back
        assert overlay.marks == (mark,)
        overlay.undo()  # undoes the draw: mark is gone again

        assert overlay.marks == ()
        assert not overlay.can_undo

    def test_committing_a_mark_after_an_erase_empties_the_redo_stack(self):
        # SNX-70 AC: "committing a new mark after an erase clears the redo
        # stack, the same rule every other action follows."
        overlay = self._overlay()
        mark = self._mark((20, 20), (80, 80))
        overlay.add_mark(mark)
        overlay.erase_at(QPointF(20, 50))
        overlay.undo()
        assert overlay.can_redo  # the erase sitting on the redo stack

        overlay.add_mark(self._mark((100, 100), (120, 120)))

        assert not overlay.can_redo

    def test_no_separate_undo_erase_method_remains(self):
        # SNX-70 AC: "no separate per-tool undo slot remains for the
        # eraser" -- erase_at's own docstring used to point at a private
        # `undo_erase`; it must be gone entirely now that undo() covers it.
        assert not hasattr(OverlayWindow, "undo_erase")


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
        assert pixel(copied, 20, 50) == self.RED  # the rectangle's left border

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
        assert pixel(before, 20, 50) != self.RED
        assert pixel(after, 20, 50) == self.RED


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
        assert pixel(saved, 5, 20) == self.RED  # the rectangle's left border


class TestToast:
    """SNX-45: the standalone `Toast` widget -- content, positioning and
    the single-instance-replaces-and-restarts-the-timer behaviour the
    spec's "Toast" section describes. `TestOverlayWindowToasts` below
    covers the four callers (`copy`/`save`/`clear`/`discard`) that drive
    this widget through `OverlayWindow._show_toast`.
    """

    def test_show_message_sets_the_icon_and_the_text(self):
        toast = Toast()

        toast.show_message("copy", "Copied to clipboard", QRectF(0, 0, 800, 600))

        assert toast._text_label.text() == "Copied to clipboard"
        assert not toast._icon_label.pixmap().isNull()
        assert toast.isVisible()

    def test_show_message_positions_bottom_centre_of_the_given_window_size(self):
        toast = Toast()
        window_size = QRectF(0, 0, 800, 600)

        toast.show_message("save", "Saved to ~/Pictures/snipux", window_size)

        size = toast.sizeHint()
        expected_left = round((window_size.width() - size.width()) / 2)
        expected_top = round(
            window_size.bottom() - tokens.Metric.TOAST_BOTTOM - size.height()
        )
        assert toast.geometry().left() == expected_left
        assert toast.geometry().top() == expected_top

    def test_dismiss_timer_interval_matches_tokens_toast_ms(self):
        toast = Toast()

        toast.show_message("trash", "Ink cleared", QRectF(0, 0, 400, 300))

        assert toast._timer.interval() == tokens.Metric.TOAST_MS
        assert toast._timer.isSingleShot()
        assert toast._timer.isActive()

    def test_toast_dismisses_itself_once_the_timer_fires(self):
        # Simulated rather than waited for real -- TOAST_MS is 2 real
        # seconds, and `overlay._advance_ants`/`_ants_timer` tests above
        # already establish the pattern of driving a QTimer's own slot
        # directly instead of blocking the suite on it.
        toast = Toast()
        toast.show_message("copy", "Copied to clipboard", QRectF(0, 0, 400, 300))
        assert toast.isVisible()

        toast._timer.timeout.emit()

        assert not toast.isVisible()

    def test_a_second_message_replaces_the_first_rather_than_stacking(self):
        toast = Toast()
        toast.show_message("copy", "Copied to clipboard", QRectF(0, 0, 400, 300))

        toast.show_message("save", "Saved to ~/Pictures/snipux", QRectF(0, 0, 400, 300))

        # One widget, its content overwritten -- not a second Toast
        # instance sitting behind or beside the first.
        assert toast._text_label.text() == "Saved to ~/Pictures/snipux"
        assert toast.isVisible()

    def test_a_second_message_restarts_the_dismiss_timer(self):
        toast = Toast()
        toast.show_message("copy", "Copied to clipboard", QRectF(0, 0, 400, 300))
        first_timer = toast._timer

        toast.show_message("save", "Saved to ~/Pictures/snipux", QRectF(0, 0, 400, 300))

        # The same QTimer instance, still running -- QTimer.start() on an
        # already-active timer resets its remaining time, which is what
        # "restarts the timer" means here rather than a fresh timer object.
        assert toast._timer is first_timer
        assert toast._timer.isActive()


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

        # 8 tools + undo + clear == 10 on the overlay's bar. The
        # destinations are one split action, which is a QWidget rather than
        # a QPushButton (two hit areas cannot be one button); the mode chip
        # and redo are built but not placed, since the handoff's
        # post-selection bar carries neither.
        visible = [button for button in buttons if not button.isHidden()]
        assert len(visible) == 10
        assert bar._action is not None
        assert bar._chip.isHidden()
        assert bar._redo_button.isHidden()

    def test_the_review_windows_bar_keeps_its_destination_pair(self):
        # Its footer already owns the exports, so a split button on the bar
        # above them would be two answers to one question.
        bar = FloatingBar(trailing="done")

        assert bar._action is None
        assert bar._copy_button is not None
        assert bar._save_button is not None

    def test_tool_buttons_cover_every_tokens_tool_in_order(self):
        bar = FloatingBar()

        assert list(bar._tool_buttons.keys()) == tokens.TOOLS

    def test_two_dividers_separate_the_overlay_bars_three_groups(self):
        # Action | tools | undo+clear. The review window keeps a third,
        # since it still carries the mode chip.
        bar = FloatingBar()

        assert len(bar.findChildren(_Divider)) == 2
        assert len(FloatingBar(trailing="done").findChildren(_Divider)) == 3

    def test_undo_redo_clear_and_the_action_are_all_present(self):
        bar = FloatingBar()

        assert bar._undo_button is not None
        assert bar._redo_button is not None
        assert bar._clear_button is not None
        # The overlay's destinations live on one split button now; the
        # review window's bar keeps the pair, since its footer owns the
        # exports and a second control would be two answers to one
        # question.
        assert bar._action is not None
        assert bar._copy_button is None

        review_bar = FloatingBar(trailing="done")
        assert review_bar._action is None
        assert review_bar._copy_button is not None
        assert review_bar._save_button is not None

    def test_bar_is_not_painted_inside_overlaywindows_paintevent(self):
        # The acceptance criterion's other half: OverlayWindow's own paint
        # pass draws the frame/scrim/ink/stroke/handles only -- see its
        # paintEvent -- and never touches `_bar` at all; the bar paints
        # itself, as a sibling layer Qt composites on top afterwards.
        frame = make_frame()
        overlay = OverlayWindow(frame)

        assert isinstance(overlay._bar, FloatingBar)
        assert overlay._bar.parent() is overlay


class TestPillButtonLabelWidth:
    """SNX-59: the capture chip read 'R' and Save showed no word at all
    because `_PillButton` sized itself off `QPushButton.sizeHint()`'s
    placeholder-text fallback (the icon/label pair lives in a child
    `QHBoxLayout` instead of the button's own text/icon, which is what
    that fallback measures) rather than off the label it actually renders.
    `grab()` is used throughout to force a real layout pass offscreen, per
    CLAUDE.md -- `sizeHint()`/`geometry()` alone can still hold stale
    pre-layout values.
    """

    def _granted_vs_hint(self, label: QLabel) -> tuple[int, int]:
        """A rendered label's granted width against its own sizeHint --
        the acceptance criterion's own measurement, factored out since
        every test below repeats it for a different pill/label pair."""
        return label.geometry().width(), label.sizeHint().width()

    def test_capture_chip_label_is_never_clipped_for_any_capture_mode(self):
        # Every tokens.CAPTURE_MODES entry, not just the "Region" default --
        # including "Full screen", the longest name and the one the ticket
        # names explicitly.
        for label, _icon, _note in tokens.CAPTURE_MODES:
            bar = FloatingBar()
            bar._chip.set_text(label)
            bar.resize(bar.sizeHint())
            bar.grab()

            granted, hint = self._granted_vs_hint(bar._chip._text_label)

            assert granted >= hint, (
                f"{label!r} asked for {hint}px but was only granted {granted}px"
            )
            assert bar._chip._text_label.text() == label

    def test_save_button_shows_the_full_word_alongside_its_icon(self):
        # The review window's bar, which still has one. The overlay's
        # destination is a split button and measured separately.
        bar = FloatingBar(trailing="done")
        bar.resize(bar.sizeHint())
        bar.grab()

        granted, hint = self._granted_vs_hint(bar._save_button._text_label)

        assert bar._save_button._text_label.text() == "Done"
        assert granted >= hint

    def test_a_clipped_label_would_fail_this_measurement(self):
        # Proves the measurement above actually bites: pinning the label to
        # a width narrower than its own sizeHint reproduces exactly the
        # clipping the ticket reports, so this test would catch a
        # regression back to a fixed-width pill.
        bar = FloatingBar()
        bar.resize(bar.sizeHint())
        bar.grab()
        label = bar._chip._text_label

        label.setFixedWidth(label.sizeHint().width() - 5)
        bar.grab()

        granted, hint = self._granted_vs_hint(label)
        assert granted < hint

    def test_pill_width_grows_with_its_own_text_rather_than_a_fixed_number(self):
        # Same construction args, only the text differs -- if the pill were
        # still sizing itself off a fixed number (or a placeholder string),
        # the two would come out equal.
        def make(text: str) -> _PillButton:
            return _PillButton(
                "chevron",
                text,
                icon_size=14,
                text_color=design_color("ACCENT_FG"),
                bg_color=design_color("ACCENT"),
                icon_after=True,
                pad_left=tokens.Metric.CHIP_PAD_L,
                pad_right=tokens.Metric.CHIP_PAD_R,
                tooltip="",
            )

        short = make("Hi")
        long = make("A very much longer capture mode name")

        assert long.sizeHint().width() > short.sizeHint().width()
        # And it should match what the actual font metrics say the label
        # needs, not some other independent guess -- the pill's sizeHint is
        # its child layout's, and the layout's own sizeHint is built from
        # each child's real sizeHint (icon label + text label + margins).
        assert short.sizeHint() == short.layout().sizeHint()
        assert long.sizeHint() == long.layout().sizeHint()

    def test_bar_still_lays_out_correctly_with_a_longer_mode_name(self):
        # "Full screen" is the longest of tokens.CAPTURE_MODES -- widening
        # the chip must push every widget after it right, with no overlap,
        # rather than clipping the label to keep the bar's old width.
        # The review window's bar: the overlay's carries no mode chip,
        # since the handoff's post-selection bars have no mode control and
        # Space reopens the chooser instead.
        bar = FloatingBar(trailing="done")
        bar.resize(bar.sizeHint())
        bar.grab()
        narrow_chip_right = bar._chip.geometry().right()
        narrow_bar_width = bar.width()

        bar._chip.set_text("Full screen")
        bar.resize(bar.sizeHint())
        bar.grab()

        assert bar._chip.geometry().right() > narrow_chip_right
        assert bar.width() > narrow_bar_width
        # The first tool button (right after the chip's divider) must not
        # overlap the now-wider chip.
        first_tool = bar._tool_buttons[tokens.TOOLS[0]]
        assert first_tool.geometry().left() >= bar._chip.geometry().right()


class TestCaptureChipResizesOnModeChange:
    """SNX-68: SNX-59 fixed `_PillButton.sizeHint` to measure the label it
    actually renders, but `FloatingBar.set_capture_mode` -- the only way
    the chip's text ever changes after construction -- never re-read that
    sizeHint, so the bar stayed sized for "Region" (the default, and the
    only label ever measured at construction) and clipped every other
    mode once picked from the popover. Unlike `TestPillButtonLabelWidth`
    above, none of these tests call `bar.resize(bar.sizeHint())`
    themselves -- that manual step is exactly what production code was
    missing, so a test that also did it wouldn't catch the regression.
    """

    def _granted_vs_hint(self, label: QLabel) -> tuple[int, int]:
        return label.geometry().width(), label.sizeHint().width()

    def test_set_capture_mode_never_clips_any_mode(self):
        # AC: "a test switches the chip through every mode and fails if
        # any label's granted width is below its sizeHint."
        bar = FloatingBar()
        bar.resize(bar.sizeHint())
        bar.grab()

        for label, _icon, _note in tokens.CAPTURE_MODES:
            bar.set_capture_mode(label)
            bar.grab()

            granted, hint = self._granted_vs_hint(bar._chip._text_label)
            assert granted >= hint, (
                f"{label!r} asked for {hint}px but was only granted {granted}px"
            )
            assert bar._chip._text_label.text() == label

    def test_set_capture_mode_grows_the_bar_for_a_longer_label(self):
        bar = FloatingBar(trailing="done")
        bar.resize(bar.sizeHint())
        bar.grab()
        narrow_width = bar.width()

        bar.set_capture_mode("Full screen")
        bar.grab()

        assert bar.width() > narrow_width

    def test_set_capture_mode_with_no_prior_reposition_still_resizes(self):
        # A bare `FloatingBar()` -- never handed a selection through
        # `reposition` -- has nothing to recentre against, but must still
        # grow to fit; this is the fallback branch of the fix.
        bar = FloatingBar()

        bar.set_capture_mode("Full screen")
        bar.grab()

        granted, hint = self._granted_vs_hint(bar._chip._text_label)
        assert granted >= hint

    def test_set_capture_mode_recentres_the_bar_under_the_selection(self):
        # AC: "the bar re-centres itself under the selection after the
        # chip changes width."
        bar = FloatingBar()
        selection = QRect(500, 300, 200, 150)
        bounds = QRectF(0, 0, 1600, 1000)
        bar.reposition(selection, bounds)
        bar.grab()
        narrow_center = bar.geometry().center().x()

        bar.set_capture_mode("Full screen")
        bar.grab()

        wide_center = bar.geometry().center().x()
        expected_center = round(selection.center().x())
        assert abs(narrow_center - expected_center) <= 1
        assert abs(wide_center - expected_center) <= 1


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
        sampled = pixel(rendered, bar.width() // 2, 2)

        expected_alpha = round(tokens.Color.BAR_BG_ALPHA * 255)
        assert sampled.alpha() == pytest.approx(expected_alpha, abs=2)
        assert (sampled.red(), sampled.green(), sampled.blue()) == QColor(
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
            pixel(rendered, x, y).alpha()
            for x in range(rect.left(), rect.right())
            for y in range(rect.top(), rect.bottom())
        ]

        # Not exactly 255: at fractional display scaling the glyph is
        # rendered large and resampled, which costs the peak a unit or
        # two. The bug this guards against is a glyph that INHERITED the
        # panel's translucency, and that lands far below this -- the fill
        # it sits on is itself well under 250.
        assert max(alphas) >= 250


class TestFloatingBarPositioning:
    """SNX-40: 'the bar is centred under the selection and clamped so it
    stays fully on screen when the selection is dragged low', per the
    spec's "Floating bar" clamp rule.
    """

    def test_centres_under_the_selection_with_room_to_spare(self):
        bar = FloatingBar()
        selection = QRect(400, 200, 200, 150)  # bottom edge at y=350
        bounds = QRectF(0, 0, 1600, 1000)

        bar.reposition(selection, bounds)

        assert bar.geometry().center().x() == pytest.approx(
            selection.center().x(), abs=1
        )
        # QRectF, not QRect.bottom(): the latter is inclusive
        # (top + height - 1), the same one-pixel trap `_bracket_path`
        # documents elsewhere in overlay.py.
        expected_top = QRectF(selection).bottom() + tokens.Metric.BAR_OFFSET_Y
        assert bar.geometry().top() == expected_top

    def test_no_room_below_flips_the_bar_above_the_selection(self):
        # The natural position (bottom + BAR_OFFSET_Y) would land past the
        # window's own bottom edge. This used to clamp the bar upward,
        # which put it *on top of the selection* -- covering the pixels the
        # user framed in order to annotate them. Reported as "when u select
        # a small region the controls are in the region so u cant edit
        # anything".
        bounds = QRectF(0, 0, 1600, 400)
        selection = QRect(400, 350, 200, 40)
        bar = FloatingBar()

        bar.reposition(selection, bounds)

        assert bar.geometry().bottom() <= QRectF(selection).top()
        assert bounds.contains(QRectF(bar.geometry()))

    def test_the_reported_strip_is_not_covered(self):
        # The exact shape from the report: a wide, 74px-tall strip sitting
        # near the bottom of a 1440-tall monitor.
        bounds = QRectF(0, 0, 2560, 1440)
        selection = QRect(700, 1330, 1123, 74)
        bar = FloatingBar()

        bar.reposition(selection, bounds)

        assert not QRectF(bar.geometry()).intersects(QRectF(selection))
        assert bounds.contains(QRectF(bar.geometry()))

    def test_a_tall_selection_ending_low_gets_the_same_answer(self):
        # Height is not what decides this -- distance from the monitor's
        # bottom edge is. A tall selection ending in the same place has the
        # same problem and must get the same treatment.
        bounds = QRectF(0, 0, 2560, 1440)
        selection = QRect(700, 300, 1123, 1104)  # bottom at 1404
        bar = FloatingBar()

        bar.reposition(selection, bounds)

        assert not QRectF(bar.geometry()).intersects(QRectF(selection))

    def test_a_selection_filling_the_monitor_still_lands_on_it(self):
        # Neither side has room, so overlap is unavoidable -- but the bar
        # must still be somewhere the user can see and press it.
        bounds = QRectF(0, 0, 1600, 400)
        selection = QRect(0, 0, 1600, 400)
        bar = FloatingBar()

        bar.reposition(selection, bounds)

        assert bounds.contains(QRectF(bar.geometry()))

    def test_room_below_is_still_preferred(self):
        # The flip is a fallback, not a new rule: anywhere there is room,
        # the bar stays where the spec puts it.
        bounds = QRectF(0, 0, 1600, 1000)
        selection = QRect(400, 200, 200, 100)
        bar = FloatingBar()

        bar.reposition(selection, bounds)

        expected = QRectF(selection).bottom() + tokens.Metric.BAR_OFFSET_Y
        assert bar.geometry().top() == expected

    def test_centre_clamps_away_from_the_left_screen_edge(self):
        bar = FloatingBar()
        bounds = QRectF(0, 0, 1600, 1000)
        selection = QRect(0, 200, 50, 50)  # centre x = 25, far left

        bar.reposition(selection, bounds)

        # abs=1: the bar's own width (sizeHint) is odd, so an exact
        # BAR_MIN_EDGE centre can land the integer geometry a pixel off
        # either side of it -- the same rounding TestReframing tolerates
        # elsewhere in this file for the same reason.
        assert bar.geometry().center().x() == pytest.approx(tokens.Metric.BAR_MIN_EDGE, abs=1)

    def test_centre_clamps_away_from_the_right_screen_edge(self):
        bar = FloatingBar()
        bounds = QRectF(0, 0, 1600, 1000)
        selection = QRect(1580, 200, 15, 50)  # centre x near the right edge

        bar.reposition(selection, bounds)

        expected = bounds.right() - tokens.Metric.BAR_MIN_EDGE
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

        overlay._bar._action.activated.emit("Copy")

        assert len(calls) == 1

    def test_save_button_click_writes_a_file(self, monkeypatch, tmp_path):
        monkeypatch.setattr(app_module.Path, "home", lambda: tmp_path)
        overlay = self._overlay(size=(50, 50))
        overlay.set_selection(QRect(0, 0, 50, 50))

        overlay._bar._action.activated.emit("Save")

        assert (tmp_path / "Pictures" / "snipux").exists()

    def test_copy_button_click_dismisses_the_overlay(self, monkeypatch):
        # SNX-62: `copy()` alone -- flatten, clipboard, toast -- used to
        # leave the overlay open, which is what let
        # AppController.start_capture()'s re-entrancy guard refuse every
        # later Snip request for the rest of the session. Mirrors
        # TestKeyboardEnter's own "and closes" test above for Enter's
        # copy-and-dismiss, but through the bar's actual button.
        monkeypatch.setattr(app_module, "copy_image_to_clipboard", lambda image: None)
        overlay = self._overlay(size=(200, 200))
        overlay.show()
        QTest.qWaitForWindowExposed(overlay)
        overlay.set_selection(QRect(0, 0, 200, 200))

        overlay._bar._action.activated.emit("Copy")

        assert not overlay.isVisible()

    def test_save_button_click_dismisses_the_overlay(self, monkeypatch, tmp_path):
        # Same fix as test_copy_button_click_dismisses_the_overlay above,
        # for Save.
        monkeypatch.setattr(app_module.Path, "home", lambda: tmp_path)
        overlay = self._overlay(size=(50, 50))
        overlay.show()
        QTest.qWaitForWindowExposed(overlay)
        overlay.set_selection(QRect(0, 0, 50, 50))

        overlay._bar._action.activated.emit("Save")

        assert not overlay.isVisible()


class TestOverlayWindowToasts:
    """SNX-45: `copy`/`save`/`clear`/`discard` each toast the message and
    glyph docs/design/overlay-redesign.md's "Toast" section names, through
    the same `_toast` instance -- `TestToast` above covers that widget's
    own content/positioning/timer behaviour in isolation.
    """

    RED = QColor(255, 0, 0)

    def _overlay(self, size=(200, 200)):
        frame = make_frame(image_size=size, logical_size=size)
        overlay = OverlayWindow(frame)
        overlay.set_selection(QRect(0, 0, *size))
        overlay.show()
        QTest.qWaitForWindowExposed(overlay)
        return overlay

    def test_copy_shows_the_copied_to_clipboard_toast(self, monkeypatch):
        monkeypatch.setattr(app_module, "copy_image_to_clipboard", lambda image: None)
        overlay = self._overlay()

        overlay.copy()

        assert overlay._toast.isVisible()
        assert overlay._toast._text_label.text() == "Copied to clipboard"

    def test_save_shows_the_saved_toast(self, monkeypatch, tmp_path):
        monkeypatch.setattr(app_module.Path, "home", lambda: tmp_path)
        overlay = self._overlay()

        overlay.save()

        assert overlay._toast.isVisible()
        assert overlay._toast._text_label.text() == "Saved to ~/Pictures/snipux"

    def test_clear_shows_the_ink_cleared_toast(self):
        overlay = self._overlay()
        overlay.add_mark(
            Rectangle(colour=self.RED, stroke_width=4, start=QPointF(0, 0), end=QPointF(10, 10))
        )

        overlay.clear()

        assert overlay.marks == ()
        assert overlay._toast.isVisible()
        assert overlay._toast._text_label.text() == "Ink cleared"

    def test_discard_shows_the_ink_discarded_toast_and_empties_the_ink_layer(self):
        overlay = self._overlay()
        overlay.add_mark(
            Rectangle(colour=self.RED, stroke_width=4, start=QPointF(0, 0), end=QPointF(10, 10))
        )

        overlay.discard()

        assert overlay.marks == ()
        assert overlay._toast.isVisible()
        assert overlay._toast._text_label.text() == "Ink discarded"

    def test_a_toast_raised_while_the_first_is_showing_replaces_it(self):
        overlay = self._overlay()
        # SNX-72: clear() is a no-op (no toast) with nothing to clear, so a
        # mark must be on screen first for it to actually fire one.
        overlay.add_mark(
            Rectangle(colour=self.RED, stroke_width=4, start=QPointF(0, 0), end=QPointF(10, 10))
        )

        overlay.clear()
        assert overlay._toast._text_label.text() == "Ink cleared"
        overlay.discard()

        # Still the one `_toast` instance -- its message overwritten, not a
        # second toast stacked alongside the first.
        assert overlay._toast._text_label.text() == "Ink discarded"

    def test_toast_stays_hidden_while_the_overlay_itself_is_not_shown(self, monkeypatch):
        # Mirrors test_bar_stays_hidden_and_unpainted_while_the_overlay_itself_is_not_shown
        # above: none of this file's other OverlayWindow pixel tests call
        # .show() before grab()ing, so a toast triggered by any of the four
        # actions below must not become a real, paintable child widget.
        monkeypatch.setattr(app_module, "copy_image_to_clipboard", lambda image: None)
        frame = make_frame(image_size=(200, 200), logical_size=(200, 200))
        overlay = OverlayWindow(frame)
        overlay.set_selection(QRect(0, 0, 200, 200))

        overlay.copy()
        overlay.clear()
        overlay.discard()

        assert not overlay._toast.isVisible()


class TestToastExcludedFromExport:
    """SNX-45 AC: 'a toast never appears in the exported image.'"""

    def test_rendered_image_is_unaffected_by_a_toast_shown_over_it(self, monkeypatch):
        monkeypatch.setattr(app_module, "copy_image_to_clipboard", lambda image: None)
        size = (600, 600)
        frame = make_frame(image_size=size, logical_size=size)
        overlay = OverlayWindow(frame)
        # The selection spans the whole window, including the toast's own
        # bottom-centre screen position -- a leak would show up there as a
        # pixel-colour mismatch against the frame's own base colour.
        overlay.set_selection(QRect(0, 0, *size))
        overlay.show()
        QTest.qWaitForWindowExposed(overlay)

        overlay.copy()
        assert overlay._toast.isVisible()  # actually on screen, not a no-op

        rendered = overlay.rendered_image()

        toast_center = overlay._toast.geometry().center()
        assert pixel(rendered, toast_center) == QColor(10, 20, 30)


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


class TestSettingsTrayFill:
    """SNX-61: 'the settings tray paints a rounded panel behind its
    controls' -- same glass treatment as FloatingBar, same alpha-not-opacity
    rule (see TestFloatingBarFill above). Before this ticket, SettingsTray
    set WA_TranslucentBackground and defined no paintEvent, so this pixel
    read as fully transparent (alpha 0) -- background, not panel.
    """

    def test_background_pixel_is_painted_at_the_token_alpha(self):
        tray = SettingsTray()
        tray.resize(tray.sizeHint())

        rendered = tray.grab().toImage()
        # Top padding strip, mid-width: inside the rounded fill but above
        # every control, so this is background only.
        sampled = pixel(rendered, tray.width() // 2, 2)

        expected_alpha = round(tokens.Color.BAR_BG_ALPHA * 255)
        assert sampled.alpha() == pytest.approx(expected_alpha, abs=2)
        assert (sampled.red(), sampled.green(), sampled.blue()) == QColor(
            tokens.Color.BAR_BG
        ).getRgb()[:3]

    def test_control_pixels_stay_fully_opaque_over_the_translucent_fill(self):
        # Painting the whole widget at reduced *opacity* would leave every
        # control pixel translucent too, at the same alpha as the
        # background -- exactly the mistake the README warns FloatingBar
        # away from. Scans a swatch button's whole rect and takes the max,
        # not the min, the same way TestFloatingBarFill's equivalent test
        # does -- a swatch is a rounded shape, so its own corner pixels sit
        # outside the fill it paints and would otherwise read as the tray's
        # translucent background rather than the button's own opacity.
        tray = SettingsTray()
        tray.resize(tray.sizeHint())

        rendered = tray.grab().toImage()
        rect = tray._swatch_buttons[tokens.INK_SWATCHES[0][1]].geometry()
        alphas = [
            pixel(rendered, x, y).alpha()
            for x in range(rect.left(), rect.right())
            for y in range(rect.top(), rect.bottom())
        ]

        assert max(alphas) == 255


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
        center = pixel(rendered, button.width() // 2, button.height() // 2)

        assert (center.red(), center.green(), center.blue()) == QColor(hex_colour).getRgb()[:3]

    def test_selected_swatch_paints_the_double_ring(self):
        tray = SettingsTray()
        _name, hex_colour = tokens.INK_SWATCHES[0]
        button = tray._swatch_buttons[hex_colour]
        button.set_selected(True)
        button.resize(button.sizeHint())

        rendered = button.grab().toImage()
        center = pixel(rendered, button.width() // 2, button.height() // 2)
        # The outermost pixel: the light ring painted flush against the
        # button's own edge, at mid-height so it falls on the ring's flat
        # side rather than its rounded corner.
        edge = pixel(rendered, 0, button.height() // 2)

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


class TestBlurTrayFill:
    """SNX-61: 'the blur tray paints the same panel treatment' as
    SettingsTray. Before this ticket, BlurTray set WA_TranslucentBackground
    and defined no paintEvent either, so this pixel read as fully
    transparent -- background, not panel.
    """

    def test_background_pixel_is_painted_at_the_token_alpha(self):
        tray = BlurTray()
        tray.resize(tray.sizeHint())

        rendered = tray.grab().toImage()
        # Top padding strip, mid-width: inside the rounded fill but above
        # every control, so this is background only.
        sampled = pixel(rendered, tray.width() // 2, 2)

        expected_alpha = round(tokens.Color.BAR_BG_ALPHA * 255)
        assert sampled.alpha() == pytest.approx(expected_alpha, abs=2)
        assert (sampled.red(), sampled.green(), sampled.blue()) == QColor(
            tokens.Color.BAR_BG
        ).getRgb()[:3]

    def test_control_pixels_stay_fully_opaque_over_the_translucent_fill(self):
        # Same rationale as TestSettingsTrayFill's equivalent test: scans
        # the active (Blur) segment button's whole rect and takes the max,
        # since its own rounded corners fall outside the segment's own
        # fill.
        tray = BlurTray()
        tray.resize(tray.sizeHint())

        rendered = tray.grab().toImage()
        rect = tray._well.blur_button.geometry()
        rect = QRect(
            tray._well.geometry().topLeft() + rect.topLeft(), rect.size()
        )
        alphas = [
            pixel(rendered, x, y).alpha()
            for x in range(rect.left(), rect.right())
            for y in range(rect.top(), rect.bottom())
        ]

        assert max(alphas) == 255


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


class TestCaptureModePopoverComposition:
    """SNX-44: the popover carries one row per `tokens.CAPTURE_MODES` entry,
    a separator, then the delay row -- per docs/design/overlay-redesign.md's
    "Capture-mode popover" section.
    """

    def test_contains_one_row_per_capture_mode_token_in_order(self):
        popover = CaptureModePopover()

        expected = [label for label, _icon, _note in tokens.CAPTURE_MODES]
        assert list(popover._rows.keys()) == expected

    def test_contains_the_separator_and_delay_row(self):
        popover = CaptureModePopover()

        assert len(popover.findChildren(_MenuSeparator)) == 1
        assert isinstance(popover._delay_row, _DelayRow)

    def test_default_delay_is_the_first_delay_token(self):
        popover = CaptureModePopover()

        assert popover.delay == tokens.DELAYS[0]
        assert popover._delay_row._value.text() == tokens.DELAYS[0]

    def test_row_label_and_note_match_their_capture_mode_token(self):
        popover = CaptureModePopover()
        label, _icon, note = tokens.CAPTURE_MODES[1]

        row = popover._rows[label]

        assert row._label.text() == label
        assert note in [child.text() for child in row.findChildren(QLabel)]


class TestCaptureModePopoverSelection:
    """SNX-44: 'the selected row is marked with a check and picking a row
    records that mode and closes the popover.'
    """

    def test_first_capture_mode_is_selected_by_default(self):
        popover = CaptureModePopover()

        default_label = tokens.CAPTURE_MODES[0][0]
        assert popover.mode == default_label
        assert popover._rows[default_label].is_selected
        assert all(
            not row.is_selected
            for label, row in popover._rows.items()
            if label != default_label
        )

    def test_clicking_a_row_selects_it_and_deselects_the_rest(self):
        popover = CaptureModePopover()
        target_label = tokens.CAPTURE_MODES[2][0]

        QTest.mouseClick(popover._rows[target_label], Qt.MouseButton.LeftButton)

        assert popover.mode == target_label
        assert popover._rows[target_label].is_selected
        assert all(
            not row.is_selected
            for label, row in popover._rows.items()
            if label != target_label
        )

    def test_clicking_a_row_emits_mode_selected(self):
        popover = CaptureModePopover()
        received = Mock()
        popover.modeSelected.connect(received)
        target_label = tokens.CAPTURE_MODES[1][0]

        QTest.mouseClick(popover._rows[target_label], Qt.MouseButton.LeftButton)

        received.assert_called_once_with(target_label)

    def test_clicking_a_row_closes_the_popover(self):
        popover = CaptureModePopover()
        popover.show()
        target_label = tokens.CAPTURE_MODES[3][0]

        QTest.mouseClick(popover._rows[target_label], Qt.MouseButton.LeftButton)

        assert not popover.isVisible()

    def test_unselected_row_shows_no_check(self):
        # isHidden(), not isVisible(): this popover is never shown here, and
        # isVisible() always reads False for a child of an unshown top-level
        # widget regardless of its own setVisible() call -- isHidden()
        # reflects the widget's own explicit show/hide state instead.
        popover = CaptureModePopover()

        other_label = tokens.CAPTURE_MODES[1][0]

        assert popover._rows[other_label]._check.isHidden()

    def test_selected_row_shows_the_check(self):
        popover = CaptureModePopover()

        default_label = tokens.CAPTURE_MODES[0][0]
        assert not popover._rows[default_label]._check.isHidden()


class TestCaptureModePopoverDelay:
    """SNX-44: 'the delay row cycles through tokens.DELAYS in order and
    wraps back to the first value.'
    """

    def test_clicking_the_delay_row_cycles_through_every_token_in_order(self):
        popover = CaptureModePopover()

        for expected in tokens.DELAYS[1:]:
            QTest.mouseClick(popover._delay_row, Qt.MouseButton.LeftButton)
            assert popover.delay == expected
            assert popover._delay_row._value.text() == expected

    def test_cycling_past_the_last_delay_wraps_to_the_first(self):
        popover = CaptureModePopover()
        for _ in range(len(tokens.DELAYS) - 1):
            QTest.mouseClick(popover._delay_row, Qt.MouseButton.LeftButton)
        assert popover.delay == tokens.DELAYS[-1]

        QTest.mouseClick(popover._delay_row, Qt.MouseButton.LeftButton)

        assert popover.delay == tokens.DELAYS[0]

    def test_clicking_the_delay_row_emits_delay_changed(self):
        popover = CaptureModePopover()
        received = Mock()
        popover.delayChanged.connect(received)

        QTest.mouseClick(popover._delay_row, Qt.MouseButton.LeftButton)

        received.assert_called_once_with(tokens.DELAYS[1])

    def test_clicking_the_delay_row_does_not_close_the_popover(self):
        popover = CaptureModePopover()
        popover.show()

        QTest.mouseClick(popover._delay_row, Qt.MouseButton.LeftButton)

        assert popover.isVisible()

    def test_clicking_the_delay_row_does_not_change_the_capture_mode(self):
        popover = CaptureModePopover()

        QTest.mouseClick(popover._delay_row, Qt.MouseButton.LeftButton)

        assert popover.mode == tokens.CAPTURE_MODES[0][0]


class TestCaptureModePopoverPositioning:
    """SNX-44: 'the popover opens above the bar when there is room above it
    and below the bar when there is not,' per the spec's rule: "if bar top
    > 300px, place the popover at bar_top - popover_height - 8; otherwise
    place it below the bar."
    """

    def test_opens_above_the_bar_when_bar_top_is_past_the_threshold(self):
        popover = CaptureModePopover()
        bar_geometry = QRect(200, 400, 600, 48)
        assert bar_geometry.top() > CaptureModePopover._UP_THRESHOLD

        popover.reposition(bar_geometry, QRectF(0, 0, 1600, 1000))

        expected_top = (
            bar_geometry.top() - popover.geometry().height() - tokens.Metric.MENU_OFFSET
        )
        assert popover.geometry().top() == expected_top
        assert popover.geometry().bottom() < bar_geometry.top()

    def test_opens_below_the_bar_when_bar_top_is_at_or_under_the_threshold(self):
        popover = CaptureModePopover()
        bar_geometry = QRect(200, 250, 600, 48)
        assert bar_geometry.top() <= CaptureModePopover._UP_THRESHOLD

        popover.reposition(bar_geometry, QRectF(0, 0, 1600, 1000))

        expected_top = bar_geometry.bottom() + tokens.Metric.MENU_OFFSET
        assert popover.geometry().top() == expected_top
        assert popover.geometry().top() > bar_geometry.bottom() - 1

    def test_width_matches_the_menu_width_token(self):
        popover = CaptureModePopover()

        popover.reposition(QRect(200, 250, 600, 48), QRectF(0, 0, 1600, 1000))

        assert popover.geometry().width() == tokens.Metric.MENU_W

    def test_horizontal_position_clamps_inside_the_window(self):
        popover = CaptureModePopover()
        bar_geometry = QRect(0, 250, 50, 48)  # far left, narrow bar

        popover.reposition(bar_geometry, QRectF(0, 0, 1600, 1000))

        assert popover.geometry().left() >= 0
        assert popover.geometry().right() <= 1600


class TestCaptureModePopoverOverlayIntegration:
    """SNX-44: the popover wired into `OverlayWindow` -- opened from the
    bar's capture chip and closed either by picking a row or by clicking
    outside it, mirroring how SNX-40/41/42 wired `FloatingBar`/the trays in.
    """

    def _overlay(self, size=(1600, 1000)):
        frame = make_frame(image_size=size, logical_size=size)
        return OverlayWindow(frame)

    def test_clicking_the_chip_opens_the_popover(self):
        overlay = self._overlay()
        overlay.show()
        QTest.qWaitForWindowExposed(overlay)
        overlay.set_selection(QRect(400, 200, 200, 150))

        QTest.mouseClick(overlay._bar._chip, Qt.MouseButton.LeftButton)

        assert overlay._popover.isVisible()

    def test_clicking_the_chip_again_closes_the_popover(self):
        overlay = self._overlay()
        overlay.show()
        QTest.qWaitForWindowExposed(overlay)
        overlay.set_selection(QRect(400, 200, 200, 150))
        QTest.mouseClick(overlay._bar._chip, Qt.MouseButton.LeftButton)
        assert overlay._popover.isVisible()

        QTest.mouseClick(overlay._bar._chip, Qt.MouseButton.LeftButton)

        assert not overlay._popover.isVisible()

    def test_popover_opens_above_the_bar_when_the_selection_sits_low(self):
        overlay = self._overlay()
        overlay.show()
        QTest.qWaitForWindowExposed(overlay)
        # A low selection pushes the bar well past the 300px threshold.
        overlay.set_selection(QRect(400, 700, 200, 150))
        assert overlay._bar.geometry().top() > CaptureModePopover._UP_THRESHOLD

        QTest.mouseClick(overlay._bar._chip, Qt.MouseButton.LeftButton)

        assert overlay._popover.geometry().bottom() < overlay._bar.geometry().top()

    def test_popover_opens_below_the_bar_when_the_selection_sits_high(self):
        overlay = self._overlay()
        overlay.show()
        QTest.qWaitForWindowExposed(overlay)
        # A high selection keeps the bar's top at or under the threshold.
        overlay.set_selection(QRect(400, 50, 200, 150))
        assert overlay._bar.geometry().top() <= CaptureModePopover._UP_THRESHOLD

        QTest.mouseClick(overlay._bar._chip, Qt.MouseButton.LeftButton)

        assert overlay._popover.geometry().top() > overlay._bar.geometry().bottom() - 1

    def test_picking_a_mode_records_it_and_updates_the_chip_label(self):
        overlay = self._overlay()
        overlay.show()
        QTest.qWaitForWindowExposed(overlay)
        overlay.set_selection(QRect(400, 200, 200, 150))
        QTest.mouseClick(overlay._bar._chip, Qt.MouseButton.LeftButton)
        # Freeform, not Window/Full screen (SNX-48 gives those two their own
        # picking/selection behaviour, covered by TestCaptureModeWindow
        # Integration/TestCaptureModeFullScreenIntegration below) -- this
        # test is only about the generic "picking a row records the label
        # and updates the chip" mechanism every row shares.
        target_label = tokens.CAPTURE_MODES[3][0]

        QTest.mouseClick(overlay._popover._rows[target_label], Qt.MouseButton.LeftButton)

        assert overlay._capture_mode == target_label
        assert overlay._bar._chip._text_label.text() == target_label
        assert not overlay._popover.isVisible()

    def test_switching_back_to_region_shrinks_and_recentres_the_bar(self):
        """SNX-68: Region's own label is short enough to fit at the bar's
        construction-time width, which is exactly why the underlying bug
        stayed invisible until a wider mode was picked -- and,
        symmetrically, why switching *back* to Region is where a bar left
        sized for that wider label would show up. Full screen (unlike
        Region) also changes `_selection`, which already forced a
        reposition through `set_selection` before this ticket; picking
        Region again changes only the label, so this is the case that
        isolates `set_capture_mode` itself needing to reposition.
        """
        overlay = self._overlay()
        overlay.show()
        QTest.qWaitForWindowExposed(overlay)
        overlay.set_selection(QRect(400, 200, 200, 150))
        # The popover is opened directly rather than through the bar: the
        # overlay's bar carries no mode chip any more (the handoff's
        # post-selection bars have no mode control), so there is nothing on
        # it to click. What this test is actually about -- that picking a
        # mode leaves the bar centred on the selection -- is unchanged.
        overlay._toggle_capture_popover()
        full_screen_label = tokens.CAPTURE_MODES[2][0]
        QTest.mouseClick(overlay._popover._rows[full_screen_label], Qt.MouseButton.LeftButton)
        expected_center = round(overlay._selection.center().x())
        assert abs(overlay._bar.geometry().center().x() - expected_center) <= 1

        overlay._toggle_capture_popover()
        region_label = tokens.CAPTURE_MODES[0][0]
        QTest.mouseClick(overlay._popover._rows[region_label], Qt.MouseButton.LeftButton)

        # Region never touches `_selection` on its own, so the selection
        # -- and therefore where the bar ought to be centred -- is
        # unchanged from the Full screen pick above.
        assert round(overlay._selection.center().x()) == expected_center
        assert abs(overlay._bar.geometry().center().x() - expected_center) <= 1

    def test_cycling_the_delay_row_updates_the_overlays_delay(self):
        overlay = self._overlay()
        overlay.show()
        QTest.qWaitForWindowExposed(overlay)
        overlay.set_selection(QRect(400, 200, 200, 150))
        QTest.mouseClick(overlay._bar._chip, Qt.MouseButton.LeftButton)

        QTest.mouseClick(overlay._popover._delay_row, Qt.MouseButton.LeftButton)

        assert overlay._delay == tokens.DELAYS[1]

    def test_clicking_outside_the_popover_closes_it_without_changing_the_mode(self):
        overlay = self._overlay()
        overlay.show()
        QTest.qWaitForWindowExposed(overlay)
        overlay.set_selection(QRect(400, 200, 200, 150))
        QTest.mouseClick(overlay._bar._chip, Qt.MouseButton.LeftButton)
        assert overlay._popover.isVisible()
        original_mode = overlay._capture_mode

        # A point on the frozen desktop, far from both the popover and any
        # other chrome -- the top-left corner is always clear of both.
        QTest.mouseClick(overlay, Qt.MouseButton.LeftButton, pos=QPoint(2, 2))

        assert not overlay._popover.isVisible()
        assert overlay._capture_mode == original_mode

    def test_popover_hides_when_the_overlay_is_hidden(self):
        overlay = self._overlay()
        overlay.show()
        QTest.qWaitForWindowExposed(overlay)
        overlay.set_selection(QRect(400, 200, 200, 150))
        QTest.mouseClick(overlay._bar._chip, Qt.MouseButton.LeftButton)
        assert overlay._popover.isVisible()

        overlay.hide()

        assert not overlay._popover.isVisible()

    def test_popover_hides_when_the_selection_is_cleared(self):
        overlay = self._overlay()
        overlay.show()
        QTest.qWaitForWindowExposed(overlay)
        overlay.set_selection(QRect(400, 200, 200, 150))
        QTest.mouseClick(overlay._bar._chip, Qt.MouseButton.LeftButton)
        assert overlay._popover.isVisible()

        overlay.set_selection(None)

        assert not overlay._popover.isVisible()


class TestShapeToolPopoverComposition:
    """SNX-64: the popover carries one row per `tokens.RECT_GROUP` entry --
    Rectangle, Ellipse, Line, Crop -- mirroring `CaptureModePopover`'s own
    "one row per token" composition.
    """

    def test_contains_one_row_per_rect_group_token_in_order(self):
        popover = ShapeToolPopover()

        assert list(popover._rows.keys()) == tokens.RECT_GROUP

    def test_row_label_and_note_match_their_tool_token(self):
        popover = ShapeToolPopover()
        tool = tokens.RECT_GROUP[1]

        row = popover._rows[tool]

        assert row._label.text() == _tool_label(tool)
        assert tokens.TOOL_HINTS[tool] in [child.text() for child in row.findChildren(QLabel)]


class TestShapeToolPopoverSelection:
    """SNX-64: the same "selected row is checked, picking a row records it
    and closes the popover" contract `CaptureModePopover` already gives
    capture modes, applied to the four rect-group shape tools.
    """

    def test_rect_is_selected_by_default(self):
        popover = ShapeToolPopover()

        assert popover.tool == "rect"
        assert popover._rows["rect"].is_selected
        assert all(
            not row.is_selected for tool, row in popover._rows.items() if tool != "rect"
        )

    def test_clicking_a_row_selects_it_and_deselects_the_rest(self):
        popover = ShapeToolPopover()

        QTest.mouseClick(popover._rows["line"], Qt.MouseButton.LeftButton)

        assert popover.tool == "line"
        assert popover._rows["line"].is_selected
        assert all(
            not row.is_selected for tool, row in popover._rows.items() if tool != "line"
        )

    def test_clicking_a_row_emits_tool_selected(self):
        popover = ShapeToolPopover()
        received = Mock()
        popover.toolSelected.connect(received)

        QTest.mouseClick(popover._rows["crop"], Qt.MouseButton.LeftButton)

        received.assert_called_once_with("crop")

    def test_clicking_a_row_closes_the_popover(self):
        popover = ShapeToolPopover()
        popover.show()

        QTest.mouseClick(popover._rows["ellipse"], Qt.MouseButton.LeftButton)

        assert not popover.isVisible()


class TestShapeToolPopoverOverlayIntegration:
    """SNX-64: the popover wired into `OverlayWindow` -- opened from the
    bar's rect button and closed either by picking a row or by clicking
    outside it, mirroring `TestCaptureModePopoverOverlayIntegration` above.
    """

    def _overlay(self, size=(1600, 1000)):
        frame = make_frame(image_size=size, logical_size=size)
        return OverlayWindow(frame)

    def test_right_clicking_the_rect_button_opens_the_popover(self):
        # Left-click is spent arming the tool, so the menu moved to the
        # other button -- picking a tool should use it, not ask which one.
        overlay = self._overlay()
        overlay.show()
        QTest.qWaitForWindowExposed(overlay)
        overlay.set_selection(QRect(400, 200, 200, 150))

        overlay._bar._tool_buttons["rect"].rightClicked.emit()

        assert overlay._shape_popover.isVisible()

    def test_left_clicking_the_rect_button_arms_it_without_a_menu(self):
        overlay = self._overlay()
        overlay.show()
        QTest.qWaitForWindowExposed(overlay)
        overlay.set_selection(QRect(400, 200, 200, 150))

        QTest.mouseClick(overlay._bar._tool_buttons["rect"], Qt.MouseButton.LeftButton)

        assert overlay._bar.active_tool == "rect"
        assert not overlay._shape_popover.isVisible()

    def test_clicking_again_advances_through_the_shape_group(self):
        # The whole group is reachable by clicking alone, and the glyph
        # follows so the button always shows what a drag will draw.
        overlay = self._overlay()
        overlay.show()
        QTest.qWaitForWindowExposed(overlay)
        overlay.set_selection(QRect(400, 200, 200, 150))
        button = overlay._bar._tool_buttons["rect"]

        seen = []
        for _ in range(len(tokens.RECT_GROUP) + 1):
            QTest.mouseClick(button, Qt.MouseButton.LeftButton)
            seen.append((overlay._bar.active_tool, button._icon_name))

        assert [tool for tool, _icon in seen] == list(tokens.RECT_GROUP) + [
            tokens.RECT_GROUP[0]
        ]
        assert all(tool == icon for tool, icon in seen), "the glyph must follow"

    def test_right_clicking_again_closes_the_popover(self):
        overlay = self._overlay()
        overlay.show()
        QTest.qWaitForWindowExposed(overlay)
        overlay.set_selection(QRect(400, 200, 200, 150))
        overlay._bar._tool_buttons["rect"].rightClicked.emit()
        assert overlay._shape_popover.isVisible()

        overlay._bar._tool_buttons["rect"].rightClicked.emit()

        assert not overlay._shape_popover.isVisible()

    def test_picking_ellipse_makes_it_the_bars_active_tool_and_closes_the_popover(self):
        overlay = self._overlay()
        overlay.show()
        QTest.qWaitForWindowExposed(overlay)
        overlay.set_selection(QRect(400, 200, 200, 150))
        overlay._bar._tool_buttons["rect"].rightClicked.emit()

        QTest.mouseClick(overlay._shape_popover._rows["ellipse"], Qt.MouseButton.LeftButton)

        assert overlay._bar.active_tool == "ellipse"
        assert not overlay._shape_popover.isVisible()

    def test_picking_a_rect_group_tool_shows_the_colour_and_stroke_tray(self):
        # Criterion: "each restored tool takes the current ink colour and
        # stroke width from the settings tray" -- which first requires the
        # tray to actually be showing once the tool is picked.
        overlay = self._overlay()
        overlay.show()
        QTest.qWaitForWindowExposed(overlay)
        overlay.set_selection(QRect(400, 200, 200, 150))
        overlay._bar._tool_buttons["rect"].rightClicked.emit()

        QTest.mouseClick(overlay._shape_popover._rows["line"], Qt.MouseButton.LeftButton)

        assert overlay._tray.isVisible()

    def test_rect_button_reads_active_while_a_group_tool_is_active(self):
        # Criterion: "the floating bar does not grow additional top-level
        # buttons" -- the rect button is still the only chrome naming the
        # group, so it has to be the one that reads active on its behalf.
        overlay = self._overlay()
        overlay.show()
        QTest.qWaitForWindowExposed(overlay)
        overlay.set_selection(QRect(400, 200, 200, 150))
        overlay._bar._tool_buttons["rect"].rightClicked.emit()

        QTest.mouseClick(overlay._shape_popover._rows["crop"], Qt.MouseButton.LeftButton)

        assert overlay._bar._tool_buttons["rect"].is_active

    def test_reopening_the_popover_shows_the_currently_active_tool_checked(self):
        overlay = self._overlay()
        overlay.show()
        QTest.qWaitForWindowExposed(overlay)
        overlay.set_selection(QRect(400, 200, 200, 150))
        QTest.mouseClick(overlay._bar._tool_buttons["rect"], Qt.MouseButton.LeftButton)
        QTest.mouseClick(overlay._shape_popover._rows["ellipse"], Qt.MouseButton.LeftButton)

        QTest.mouseClick(overlay._bar._tool_buttons["rect"], Qt.MouseButton.LeftButton)

        assert overlay._shape_popover._rows["ellipse"].is_selected

    def test_clicking_outside_the_popover_closes_it_without_changing_the_tool(self):
        overlay = self._overlay()
        overlay.show()
        QTest.qWaitForWindowExposed(overlay)
        overlay.set_selection(QRect(400, 200, 200, 150))
        overlay._bar._tool_buttons["rect"].rightClicked.emit()
        assert overlay._shape_popover.isVisible()
        original_tool = overlay._bar.active_tool

        # A point on the frozen desktop, far from both the popover and any
        # other chrome -- the top-left corner is always clear of both.
        QTest.mouseClick(overlay, Qt.MouseButton.LeftButton, pos=QPoint(2, 2))

        assert not overlay._shape_popover.isVisible()
        assert overlay._bar.active_tool == original_tool

    def test_popover_hides_when_the_overlay_is_hidden(self):
        overlay = self._overlay()
        overlay.show()
        QTest.qWaitForWindowExposed(overlay)
        overlay.set_selection(QRect(400, 200, 200, 150))
        overlay._bar._tool_buttons["rect"].rightClicked.emit()
        assert overlay._shape_popover.isVisible()

        overlay.hide()

        assert not overlay._shape_popover.isVisible()

    def test_popover_hides_when_the_selection_is_cleared(self):
        overlay = self._overlay()
        overlay.show()
        QTest.qWaitForWindowExposed(overlay)
        overlay.set_selection(QRect(400, 200, 200, 150))
        overlay._bar._tool_buttons["rect"].rightClicked.emit()
        assert overlay._shape_popover.isVisible()

        overlay.set_selection(None)

        assert not overlay._shape_popover.isVisible()


class TestCaptureModeRowSizing:
    """SNX-75: `_CaptureModeRow` and `_DelayRow` are `QPushButton`s whose
    real content -- glyph, two-line label, check mark -- lives in a child
    layout rather than the button's own text()/icon(), the same shape
    `_PillButton` was in for SNX-59. Left unfixed, `QPushButton.sizeHint()`
    falls back to its placeholder-text measurement (48x12 per the ticket)
    instead of the ~45px a 12.5px name over an 11px note plus top/bottom
    padding actually needs, and the popover's QVBoxLayout collapses every
    row to that sliver.
    """

    def test_capture_mode_row_size_hint_matches_its_child_layout(self):
        row = _CaptureModeRow("Region", "crop", "Drag any rectangle")

        assert row.sizeHint() == row.layout().sizeHint()
        assert row.minimumSizeHint() == row.sizeHint()

    def test_delay_row_size_hint_matches_its_child_layout(self):
        row = _DelayRow()

        assert row.sizeHint() == row.layout().sizeHint()
        assert row.minimumSizeHint() == row.sizeHint()

    def test_row_height_grows_with_the_font_it_actually_renders_with(self, monkeypatch):
        # Acceptance criterion: "a row's height comes from the fonts and
        # metrics it renders with, not a fixed number." Bumping the label
        # font's own pixel size (read at construction time, same as every
        # other row) must grow the row's sizeHint in step -- a fixed-number
        # height wouldn't move at all.
        small = _CaptureModeRow("Region", "crop", "Drag any rectangle")

        monkeypatch.setattr(tokens.Font, "MENU_LABEL", (30.0, 500))
        big = _CaptureModeRow("Region", "crop", "Drag any rectangle")

        assert big.sizeHint().height() > small.sizeHint().height()

    def test_row_is_tall_enough_for_its_glyph_and_two_line_label(self):
        # The ticket's own arithmetic, computed from the real fonts rather
        # than restated as a literal: a 12.5px name over an 11px note (with
        # the row's own inter-line gap) versus the 16px glyph, whichever is
        # taller, plus MENU_ROW_PAD_V top and bottom.
        row = _CaptureModeRow("Full screen", "monitor", "Whole display")
        row.resize(row.sizeHint())
        row.grab()
        metric = tokens.Metric

        label_font = QFont(font_families().ui)
        size, weight = tokens.Font.MENU_LABEL
        label_font.setPixelSize(round(size))
        label_font.setWeight(QFont.Weight(weight))

        note_font = QFont(font_families().ui)
        size, weight = tokens.Font.MENU_NOTE
        note_font.setPixelSize(round(size))
        note_font.setWeight(QFont.Weight(weight))

        text_height = (
            QFontMetricsF(label_font).height()
            + row._LABEL_GAP
            + QFontMetricsF(note_font).height()
        )
        content_height = max(row._ICON_SIZE, text_height)
        min_height = content_height + 2 * metric.MENU_ROW_PAD_V

        assert row.height() >= min_height


class TestPopoverHeightReflectsItsChildren:
    """SNX-75 acceptance: 'the popover's own height is the sum of its rows,
    separator and padding rather than a collapsed value' -- checked against
    the actual laid-out children, not a hand re-derived number, so this
    would fail the same way the ticket's own 262x83 measurement did before
    the row fix.
    """

    def test_capture_mode_popover_height_equals_its_rows_plus_separator_plus_padding(self):
        popover = CaptureModePopover()
        popover.resize(popover.sizeHint())
        popover.grab()
        metric = tokens.Metric

        separator = popover.findChild(_MenuSeparator)
        children_height = (
            sum(row.height() for row in popover._rows.values())
            + separator.height()
            + popover._delay_row.height()
        )

        assert popover.height() == children_height + 2 * metric.MENU_PAD

    def test_capture_mode_popover_height_is_no_longer_collapsed(self):
        # Before SNX-75, five rows collapsed to ~12px apiece (see
        # `_CaptureModeRow`'s own docstring for the ticket's 262x83
        # measurement) -- comfortably under 100px total. A real popover with
        # four two-line mode rows, a separator and a delay row needs well
        # over that.
        popover = CaptureModePopover()

        assert popover.sizeHint().height() > 150

    def test_shape_tool_popover_height_equals_its_rows_plus_padding(self):
        popover = ShapeToolPopover()
        popover.resize(popover.sizeHint())
        popover.grab()
        metric = tokens.Metric

        children_height = sum(row.height() for row in popover._rows.values())

        assert popover.height() == children_height + 2 * metric.MENU_PAD

    def test_shape_tool_popover_height_is_no_longer_collapsed(self):
        popover = ShapeToolPopover()

        assert popover.sizeHint().height() > 100

    def test_reposition_uses_the_corrected_uncollapsed_height(self):
        # AC: "the popover still opens above the bar when there is room and
        # below it when there is not, at its corrected height" -- both
        # branches already read `self.sizeHint().height()`, so the fix here
        # is just that height no longer being a collapsed value.
        popover = CaptureModePopover()
        bar_geometry = QRect(200, 400, 600, 48)
        assert bar_geometry.top() > CaptureModePopover._UP_THRESHOLD

        popover.reposition(bar_geometry, QRectF(0, 0, 1600, 1000))

        assert popover.geometry().height() > 150
        assert popover.geometry().bottom() < bar_geometry.top()


class TestPopoverChildrenAreNeverClippedBelowSizeHint:
    """SNX-75 acceptance: 'a test opens each popover and fails if any child
    is laid out smaller than its sizeHint.' `grab()` forces a real layout
    pass offscreen, per CLAUDE.md and mirroring `TestPillButtonLabelWidth`'s
    own convention -- `sizeHint()`/`geometry()` alone can hold stale
    pre-layout values.

    Height only, not width: every row is deliberately stretched to the
    popover's own fixed `MENU_W` column by the parent `QVBoxLayout`
    (`CaptureModePopover`/`ShapeToolPopover` both `setFixedWidth`), the same
    way `_MenuSeparator`'s width is never its own sizeHint's either -- width
    is a layout choice, not a symptom of the collapse this ticket fixes.
    The defect this test guards, per the ticket's own measurements, is
    rows/popovers laid out *shorter* than the content they render.
    """

    def _assert_no_child_is_clipped(self, popover: QWidget) -> None:
        for child in popover.findChildren(QWidget):
            granted = child.geometry().height()
            hint = child.sizeHint().height()
            assert granted >= hint, (
                f"{child!r} granted {granted}px tall but its own sizeHint asks for {hint}px"
            )

    def test_capture_mode_popover_opens_with_no_child_clipped(self):
        popover = CaptureModePopover()
        popover.resize(popover.sizeHint())
        popover.grab()

        self._assert_no_child_is_clipped(popover)

    def test_shape_tool_popover_opens_with_no_child_clipped(self):
        popover = ShapeToolPopover()
        popover.resize(popover.sizeHint())
        popover.grab()

        self._assert_no_child_is_clipped(popover)

    def test_a_collapsed_row_would_fail_this_measurement(self):
        # Proves the measurement above actually bites: forcing a row back
        # down to a fixed height shorter than its own sizeHint reproduces
        # exactly the collapse the ticket reports.
        popover = CaptureModePopover()
        popover.resize(popover.sizeHint())
        popover.grab()
        row = next(iter(popover._rows.values()))

        row.setFixedHeight(12)
        popover.grab()

        with pytest.raises(AssertionError):
            self._assert_no_child_is_clipped(popover)


def _close_stray_toplevel_windows() -> None:
    """Close every top-level widget still alive from an earlier test.

    None of this file's many other `OverlayWindow`/`Overlay` tests close
    their own instance -- ordinary practice throughout the file, since
    each test builds a fresh one and nothing downstream reads a previous
    test's leftovers. But `TestCaptureModeWindowIntegration`/
    `TestCaptureModeFullScreenIntegration` below depend on a bare hover-
    only `QTest.mouseMove` (no button held) actually reaching the right
    window -- and by the time hundreds of never-closed, same-screen-rect
    top-level windows have piled up over a full run, the offscreen QPA
    platform can misroute that hover to a stale one instead of the
    current test's, a real flake this codebase doesn't otherwise trigger
    (every other interaction in this file is press-driven, which grabs
    the mouse and isn't affected). A clean slate immediately before each
    test in those two classes only -- not a file-wide fixture, so no
    other test's behaviour changes -- keeps that routing unambiguous.
    """
    for widget in QApplication.topLevelWidgets():
        widget.close()


class TestCaptureModeWindowIntegration:
    """SNX-48 AC: picking Window in the popover arms hover-preview/click-
    to-snap picking on `OverlayWindow` itself -- sourced from a
    `GeometryProvider`, the same one `Overlay`'s own WINDOW mode
    (`TestWindowMode` above) already uses -- producing a `_selection`
    that stays open for re-framing and in-place annotation instead of
    being confirmed into a separate editor.
    """

    @pytest.fixture(autouse=True)
    def _clean_slate(self):
        _close_stray_toplevel_windows()

    # y=90, not 30: while `_picking_window` is armed the prior selection is
    # None, which is also `Chooser`'s own "nothing chosen yet" condition
    # (`_sync_chooser_visibility`), so its panel -- SNX-120's stills/record
    # switch widened it -- is up too, spanning the full top ChooserMetric.
    # HEIGHT (54px) band. A rect/point inside that band never reaches
    # `OverlayWindow.mouseMoveEvent` at all (Qt delivers a bare hover to
    # whichever child sits under the cursor instead), silently turning a
    # hit into a no-op. 90 clears it with margin.
    WINDOW_RECT = QRectF(30, 90, 50, 50)
    HIT_POINT = QPoint(50, 110)
    # Outside WINDOW_RECT, and -- as important -- outside the real child
    # widgets `OverlayWindow` shows once it has a selection: `HintHUD`
    # spans the full width for `tokens.Metric.HUD_H` (44px) from the top,
    # and the floating bar sits well below the selection. A point inside
    # either one would never reach `OverlayWindow.mouseMoveEvent` at all
    # (Qt delivers it to that child instead), silently turning this into
    # a no-op rather than an actual miss.
    MISS_POINT = QPoint(200, 70)
    WINDOW_LABEL = tokens.CAPTURE_MODES[1][0]
    REGION_LABEL = tokens.CAPTURE_MODES[0][0]

    def _overlay(self, provider=None, size=(600, 600)):
        frame = make_frame(image_size=size, logical_size=size)
        overlay = OverlayWindow(frame, geometry_provider=provider)
        # The popover is only reachable through the bar's own chip, and
        # the bar is only shown once a selection already exists -- so
        # every scenario below starts from an ordinary prior selection,
        # the same way a real capture would already have one (a default
        # Region selection, or an earlier drag) before the user ever
        # opens the mode chip.
        overlay.set_selection(QRect(400, 200, 100, 100))
        overlay.show()
        QTest.qWaitForWindowExposed(overlay)
        # A window's very first bare hover-only QTest.mouseMove (no button
        # held) can be dropped by the offscreen QPA platform even with a
        # clean slate (`_clean_slate` above); a throwaway move here, before
        # any test's own real one, reliably establishes hover delivery to
        # *this* window for the rest of the test.
        QTest.mouseMove(overlay, QPoint(0, 0))
        return overlay

    def _pick_window_mode(self, overlay):
        QTest.mouseClick(overlay._bar._chip, Qt.MouseButton.LeftButton)
        QTest.mouseClick(
            overlay._popover._rows[self.WINDOW_LABEL], Qt.MouseButton.LeftButton
        )

    def test_picking_window_arms_picking_and_clears_the_prior_selection(self):
        overlay = self._overlay(_FakeWindowProvider(self.WINDOW_RECT))

        self._pick_window_mode(overlay)

        assert overlay._picking_window
        assert overlay._selection is None

    def test_hover_previews_the_window_under_the_cursor(self):
        overlay = self._overlay(_FakeWindowProvider(self.WINDOW_RECT))
        self._pick_window_mode(overlay)

        QTest.mouseMove(overlay, self.HIT_POINT)

        assert overlay._selection == self.WINDOW_RECT.toRect()

    def test_hover_clears_on_a_miss(self):
        overlay = self._overlay(_FakeWindowProvider(self.WINDOW_RECT))
        self._pick_window_mode(overlay)
        QTest.mouseMove(overlay, self.HIT_POINT)
        assert overlay._selection is not None

        QTest.mouseMove(overlay, self.MISS_POINT)

        assert overlay._selection is None

    def test_click_snaps_the_selection_and_disarms_picking(self):
        overlay = self._overlay(_FakeWindowProvider(self.WINDOW_RECT))
        self._pick_window_mode(overlay)

        QTest.mouseClick(overlay, Qt.MouseButton.LeftButton, pos=self.HIT_POINT)

        assert overlay._selection == self.WINDOW_RECT.toRect()
        assert not overlay._picking_window

    def test_click_on_a_miss_leaves_picking_armed(self):
        overlay = self._overlay(_FakeWindowProvider(self.WINDOW_RECT))
        self._pick_window_mode(overlay)

        QTest.mouseClick(overlay, Qt.MouseButton.LeftButton, pos=self.MISS_POINT)

        assert overlay._picking_window
        assert overlay._selection is None

    def test_selection_from_window_mode_is_reframable_like_a_dragged_one(self):
        # Mirrors TestDimensionChipLiveUpdate's own pattern above: `press`
        # only needs to land inside the handle to grab it -- the resize
        # itself is driven entirely by the absolute `target` passed to the
        # move/release that follow, not by any delta from `press`.
        overlay = self._overlay(_FakeWindowProvider(self.WINDOW_RECT))
        self._pick_window_mode(overlay)
        QTest.mouseClick(overlay, Qt.MouseButton.LeftButton, pos=self.HIT_POINT)
        press = overlay._edge_handle_rect(Handle.RIGHT).center().toPoint()
        target = QPoint(120, 115)

        QTest.mousePress(overlay, Qt.MouseButton.LeftButton, pos=press)
        QTest.mouseMove(overlay, target)
        QTest.mouseRelease(overlay, Qt.MouseButton.LeftButton, pos=target)

        # left/top/bottom stay anchored at the window-mode rect's own
        # edges (30, 90, 80); only the dragged right edge moves, to 120.
        assert overlay._selection == QRect(30, 90, 90, 50)

    def test_selection_from_window_mode_can_be_annotated(self):
        overlay = self._overlay(_FakeWindowProvider(self.WINDOW_RECT))
        self._pick_window_mode(overlay)
        QTest.mouseClick(overlay, Qt.MouseButton.LeftButton, pos=self.HIT_POINT)

        overlay.add_mark(_mark())

        assert len(overlay.marks) == 1

    def test_no_provider_toasts_and_falls_back_to_region(self):
        # `geometry_provider=None` -> `UnsupportedGeometryProvider`, per
        # `OverlayWindow`'s own default -- the same "degrade rather than
        # raise" fallback `Overlay`'s WINDOW mode already has, but this
        # ticket also requires telling the user rather than a silent no-op.
        overlay = self._overlay(provider=None)

        self._pick_window_mode(overlay)

        assert not overlay._picking_window
        assert overlay._capture_mode == self.REGION_LABEL
        assert overlay._bar._chip._text_label.text() == self.REGION_LABEL
        assert overlay._popover.mode == self.REGION_LABEL
        # The chooser is the third surface showing this value: leaving it
        # on "Window" would have its tab naming a mode that was just
        # refused while the chip below it read "Region".
        assert overlay._chooser.mode == self.REGION_LABEL
        assert overlay._toast.isVisible()
        assert "window" in overlay._toast._text_label.text().lower()

    def test_unavailable_provider_toasts_and_never_queries_window_at(self):
        provider = Mock(spec=GeometryProvider)
        provider.is_available.return_value = False
        overlay = self._overlay(provider=provider)

        self._pick_window_mode(overlay)

        assert not overlay._picking_window
        assert overlay._capture_mode == self.REGION_LABEL
        assert overlay._toast.isVisible()
        provider.window_at.assert_not_called()


class TestInstantCapture:
    """`instant` finishes the snip the moment the selection is made --
    no overlay to dismiss, no button to press.

    The other two answers to "then" both leave the frozen frame up; this
    is the only one that ends the session itself, so what it must never do
    is end it early. A live drag calls `set_selection` on every mouse
    move, and finishing on the first pixel of one is not instant capture,
    it is a broken drag.
    """

    @pytest.fixture(autouse=True)
    def _clean_slate(self):
        _close_stray_toplevel_windows()

    def _overlay(self, outcome="instant", size=(800, 600)):
        frame = make_frame(image_size=size, logical_size=size)
        overlay = OverlayWindow(
            frame, geometry_provider=_FakeWindowProvider(QRectF(30, 30, 200, 150))
        )
        overlay.setGeometry(0, 0, *size)
        overlay.show()
        QTest.qWaitForWindowExposed(overlay)
        overlay._chooser.set_after(outcome)
        return overlay

    def _drag(self, overlay, start, end):
        QTest.mousePress(overlay, Qt.MouseButton.LeftButton, pos=start)
        QTest.mouseMove(overlay, QPoint((start.x() + end.x()) // 2, (start.y() + end.y()) // 2))
        QTest.mouseMove(overlay, end)
        QTest.mouseRelease(overlay, Qt.MouseButton.LeftButton, pos=end)

    def test_a_region_drag_copies_and_closes_on_release(self, monkeypatch):
        copied = []
        monkeypatch.setattr(app_module, "copy_image_to_clipboard", copied.append)
        overlay = self._overlay()

        self._drag(overlay, QPoint(100, 100), QPoint(400, 350))

        assert len(copied) == 1
        assert copied[0].size() == QSize(300, 250)
        assert not overlay.isVisible()

    def test_nothing_is_copied_part_way_through_the_drag(self, monkeypatch):
        # The whole reason this hangs off a commit funnel and not
        # `set_selection`, which runs on every move of a live drag.
        copied = []
        monkeypatch.setattr(app_module, "copy_image_to_clipboard", copied.append)
        overlay = self._overlay()

        QTest.mousePress(overlay, Qt.MouseButton.LeftButton, pos=QPoint(100, 100))
        QTest.mouseMove(overlay, QPoint(250, 220))
        QTest.mouseMove(overlay, QPoint(400, 350))

        assert copied == []
        assert overlay.isVisible()

    def test_a_drag_too_small_to_commit_copies_nothing(self, monkeypatch):
        # Below the 16x16 floor is a discarded misfire, not a snip -- and
        # a misfire that copied the screen and vanished would be the worst
        # possible reading of "instant".
        copied = []
        monkeypatch.setattr(app_module, "copy_image_to_clipboard", copied.append)
        overlay = self._overlay()

        self._drag(overlay, QPoint(100, 100), QPoint(105, 104))

        assert copied == []
        assert overlay.isVisible()

    def test_picking_a_window_copies_it_immediately(self, monkeypatch):
        copied = []
        monkeypatch.setattr(app_module, "copy_image_to_clipboard", copied.append)
        overlay = self._overlay()
        overlay._chooser.set_mode("Window")

        QTest.mouseClick(overlay, Qt.MouseButton.LeftButton, pos=QPoint(100, 100))

        assert len(copied) == 1
        assert not overlay.isVisible()

    def test_full_screen_copies_without_a_click_at_all(self, monkeypatch):
        copied = []
        monkeypatch.setattr(app_module, "copy_image_to_clipboard", copied.append)
        overlay = self._overlay()

        overlay._chooser.set_mode("Full screen")

        assert len(copied) == 1
        assert not overlay.isVisible()

    def test_edit_leaves_the_frame_up_with_the_bar_on_it(self, monkeypatch):
        # The default, and the behaviour every version before this had.
        copied = []
        monkeypatch.setattr(app_module, "copy_image_to_clipboard", copied.append)
        overlay = self._overlay(outcome="edit")

        self._drag(overlay, QPoint(100, 100), QPoint(400, 350))

        assert copied == []
        assert overlay.isVisible()
        assert overlay._bar.isVisibleTo(overlay)

    def test_review_leaves_the_frame_up_too(self, monkeypatch):
        # `review` is about what opens *after* the overlay, so the overlay
        # itself behaves exactly as `edit` does.
        copied = []
        monkeypatch.setattr(app_module, "copy_image_to_clipboard", copied.append)
        overlay = self._overlay(outcome="review")

        self._drag(overlay, QPoint(100, 100), QPoint(400, 350))

        assert copied == []
        assert overlay.isVisible()

    def test_it_reports_the_capture_the_same_way_copy_always_has(self, monkeypatch):
        # `app.py` opens the review window off this hook; instant is the
        # ordinary Copy path, so it reports like one.
        monkeypatch.setattr(app_module, "copy_image_to_clipboard", lambda image: None)
        reported = []
        frame = make_frame(image_size=(800, 600), logical_size=(800, 600))
        overlay = OverlayWindow(
            frame, on_captured=lambda image, path: reported.append((image, path))
        )
        overlay.setGeometry(0, 0, 800, 600)
        overlay.show()
        QTest.qWaitForWindowExposed(overlay)
        overlay._chooser.set_after("instant")

        self._drag(overlay, QPoint(100, 100), QPoint(400, 350))

        assert len(reported) == 1
        assert reported[0][1] is None, "nothing was written to disk"

    def test_instant_saves_setting_writes_a_file_instead_of_copying(self, monkeypatch, tmp_path):
        # SNX-111 AC: kept, and honoured -- Instant can now be asked to
        # save without copying, which is the "Save silently" destination
        # the old three-way menu (review/clip/file) lost when `clip` and
        # `file` were collapsed into today's single `instant`.
        monkeypatch.setattr(overlay_module.setup_desktop, "load_instant_saves", lambda *a, **k: True)
        monkeypatch.setattr(app_module.Path, "home", lambda: tmp_path)
        copied = []
        monkeypatch.setattr(app_module, "copy_image_to_clipboard", copied.append)
        overlay = self._overlay()

        self._drag(overlay, QPoint(100, 100), QPoint(400, 350))

        assert copied == [], "the whole point is that it does not also copy"
        assert (tmp_path / "Pictures" / "snipux").exists()
        assert not overlay.isVisible()


class TestCommitToRecord:
    """SNX-122 AC: on the record side of the chooser, `_commit_selection`
    hands the rect off to `on_recording_requested` (absolute coordinates,
    None for Full screen) rather than running any of the stills-only
    `outcome` handling right below it.

    It used to close the overlay here too. It no longer does: the
    handoff's ready stage is the one place a recording can still be
    reframed, and the handles that reframe it are this window's, so the
    window stays up with the stills bar suppressed until `app.py` starts
    the backend. See `_commit_selection`'s record branch.
    """

    @pytest.fixture(autouse=True)
    def _clean_slate(self, monkeypatch):
        _close_stray_toplevel_windows()
        # `Chooser.kindChanged` is wired straight to `setup_desktop.save_kind`
        # (SNX-122's own `_chooser.set_kind("record")` below fires it), which
        # would otherwise write to this machine's real config file the same
        # way a real session does. `load_kind` is pinned the same way so a
        # write a previous, unrelated test left behind can't change which
        # side a fresh `OverlayWindow` opens on here.
        monkeypatch.setattr(overlay_module.setup_desktop, "save_kind", lambda *a, **k: True)
        monkeypatch.setattr(overlay_module.setup_desktop, "load_kind", lambda *a, **k: "stills")

    def _overlay(
        self,
        on_recording_requested=None,
        logical_origin=(0, 0),
        monitor_geometries=None,
        size=(600, 600),
    ):
        frame = make_frame(
            image_size=size, logical_size=size, logical_origin=logical_origin
        )
        overlay = OverlayWindow(
            frame,
            monitor_geometries=monitor_geometries,
            on_recording_requested=on_recording_requested,
        )
        overlay._chooser.set_kind("record")
        overlay.setGeometry(0, 0, *size)
        overlay.show()
        QTest.qWaitForWindowExposed(overlay)
        return overlay

    def _drag(self, overlay, start, end):
        QTest.mousePress(overlay, Qt.MouseButton.LeftButton, pos=start)
        QTest.mouseMove(overlay, QPoint((start.x() + end.x()) // 2, (start.y() + end.y()) // 2))
        QTest.mouseMove(overlay, end)
        QTest.mouseRelease(overlay, Qt.MouseButton.LeftButton, pos=end)

    def test_a_region_drag_hands_off_the_absolute_rect_and_stays_up(self):
        requests = []
        overlay = self._overlay(on_recording_requested=lambda rect, delay, after: requests.append((rect, delay, after)))

        self._drag(overlay, QPoint(100, 100), QPoint(400, 350))

        assert len(requests) == 1
        rect, delay, after = requests[0]
        assert rect == QRectF(100, 100, 300, 250)
        assert delay == tokens.DELAYS[0]  # "No delay", the default
        assert after == tokens.RECORD_AFTER_DEFAULT  # "instant", the chooser's own default
        # Still up, and armed: the region has to stay reframeable until
        # recording actually starts, and the stills bar has no business
        # being on a recording.
        assert overlay.isVisible()
        assert overlay._armed_for_recording is True
        assert overlay._bar.isHidden() is True

    def test_a_region_drag_on_a_monitor_left_of_and_above_the_primary_translates_the_rect(self):
        # The case a naive `abs()` on `logical_origin` gets wrong: this
        # monitor's origin is negative on both axes, so the absolute rect
        # must be a real translate, not a mirror.
        requests = []
        overlay = self._overlay(
            on_recording_requested=lambda rect, delay, after: requests.append((rect, delay, after)),
            logical_origin=(-500, -300),
            monitor_geometries=[
                QRectF(-500, -300, 600, 600),
                QRectF(100, -300, 800, 600),
            ],
        )

        self._drag(overlay, QPoint(100, 100), QPoint(400, 350))

        assert len(requests) == 1
        rect, _delay, _after = requests[0]
        assert rect == QRectF(-400, -200, 300, 250)

    def test_full_screen_hands_off_the_monitor_rather_than_every_monitor(self):
        # This asserted the opposite until it was run on a real
        # three-monitor desktop. Handing over None makes the backend record
        # the whole *virtual* desktop -- one 6400x1440 file of all three --
        # while the identical row on the stills side captures one display,
        # and the chooser promises "Grabs this monitor the moment you
        # choose it". `_select_full_screen` has already picked the display
        # under the cursor, so Full screen is a region like any other here.
        requests = []
        overlay = self._overlay(on_recording_requested=lambda rect, delay, after: requests.append((rect, delay, after)))

        overlay._chooser.set_mode("Full screen")

        assert len(requests) == 1
        rect, _delay, _after = requests[0]
        assert rect is not None
        assert rect == overlay.absolute_selection()

    def test_the_dimension_chip_drops_the_mark_count_for_a_recording(self):
        # "793 x 458 - 0 marks" over a region about to be filmed counts a
        # feature that does not apply to it: there is no annotate-in-place
        # for a video.
        overlay = self._overlay(on_recording_requested=lambda rect, delay, after: None)
        overlay.set_selection(QRectF(10, 10, 793, 458))

        size, marks = overlay._dimension_chip_texts()

        assert size == "793 × 458"
        assert marks == ""

        # And the chip shrinks to fit: reserving the middot's width would
        # pad the chip out around a separator with nothing on the far side
        # of it, which reads as a truncated label.
        record_width = overlay._dimension_chip_rect().width()
        overlay._chooser.set_kind("stills")
        stills_width = overlay._dimension_chip_rect().width()
        assert record_width < stills_width

    def test_the_stills_side_still_counts_its_marks(self):
        frame = make_frame(image_size=(600, 600), logical_size=(600, 600))
        overlay = OverlayWindow(frame)
        overlay.setGeometry(0, 0, 600, 600)
        overlay.set_selection(QRectF(10, 10, 793, 458))

        _size, marks = overlay._dimension_chip_texts()

        assert marks == "0 marks"

    def test_full_screen_arms_like_any_other_region(self):
        # It is a monitor-sized selection now, not a special case, so it
        # gets the same handles as a dragged one -- which is what makes
        # "full screen, but a bit narrower" reachable without starting the
        # snip again.
        overlay = self._overlay(on_recording_requested=lambda rect, delay, after: None)

        overlay._chooser.set_mode("Full screen")

        assert overlay.isVisible()
        assert overlay._armed_for_recording is True

    def test_the_armed_region_can_still_be_reframed(self):
        # The whole reason the window stays up. `absolute_selection()` is
        # what app.py reads when recording actually starts, so a rect
        # changed here is the rect that gets filmed -- otherwise the ready
        # stage's handles would be a lie.
        overlay = self._overlay(on_recording_requested=lambda rect, delay, after: None)
        self._drag(overlay, QPoint(100, 100), QPoint(400, 350))
        assert overlay.absolute_selection() == QRectF(100, 100, 300, 250)

        overlay.set_selection(QRectF(120, 130, 200, 180))

        assert overlay.absolute_selection() == QRectF(120, 130, 200, 180)

    def test_the_armed_delay_reaches_the_callback_unchanged(self):
        # This ticket doesn't own the timer that counts the delay down --
        # app.py does -- so the assertion is "the right value reaches the
        # callback", not "N seconds elapse".
        requests = []
        overlay = self._overlay(on_recording_requested=lambda rect, delay, after: requests.append((rect, delay, after)))
        overlay._on_delay_changed(tokens.DELAYS[1])  # "3s"

        self._drag(overlay, QPoint(100, 100), QPoint(400, 350))

        assert requests[0][1] == tokens.DELAYS[1]

    @pytest.mark.parametrize("after", ["instant", "save"])
    def test_the_chosen_after_reaches_the_callback_as_a_third_argument(self, after):
        # SNX-124 ticket 9: app.py's `_on_recording_requested` needs to know
        # which of record's "then" vocabulary was picked so it can land or
        # copy the finished file accordingly -- this is the handoff half of
        # that, `outcome` (== `self._chooser.after`) passed through, not
        # dropped on the floor the way it used to be.
        requests = []
        overlay = self._overlay(
            on_recording_requested=lambda rect, delay, after: requests.append(after)
        )
        overlay._chooser.set_after(after)

        self._drag(overlay, QPoint(100, 100), QPoint(400, 350))

        assert requests == [after]

    @pytest.mark.parametrize("after", ["instant", "save"])
    def test_arms_regardless_of_after(self, after):
        # `outcome`/`after` decide what app.py does with the file once it
        # is real; they play no part in what this window does, which is
        # stay up and reframeable either way.
        overlay = self._overlay(on_recording_requested=lambda rect, delay, after: None)
        overlay._chooser.set_after(after)

        self._drag(overlay, QPoint(100, 100), QPoint(400, 350))

        assert overlay.isVisible()
        assert overlay._armed_for_recording is True

    def test_stills_kind_never_calls_the_recording_callback(self):
        # Kind defaults to "stills" -- proving the branch added for
        # SNX-122 is truly gated on it, not just usually not reached.
        requests = []
        frame = make_frame(image_size=(600, 600), logical_size=(600, 600))
        overlay = OverlayWindow(
            frame, on_recording_requested=lambda rect, delay, after: requests.append((rect, delay, after))
        )
        overlay._chooser.set_after("edit")  # stays open; nothing else to assert on
        overlay.setGeometry(0, 0, 600, 600)
        overlay.show()
        QTest.qWaitForWindowExposed(overlay)

        self._drag(overlay, QPoint(100, 100), QPoint(400, 350))

        assert requests == []


class TestCaptureModeFullScreenIntegration:
    """SNX-48 AC: picking Full screen in the popover sets `_selection` to
    the whole display the cursor is on, immediately -- no drag or click
    needed past picking the row.
    """

    @pytest.fixture(autouse=True)
    def _clean_slate(self):
        _close_stray_toplevel_windows()

    FULL_SCREEN_LABEL = tokens.CAPTURE_MODES[2][0]

    def _overlay(self, size=(600, 600), monitor_geometries=None):
        frame = make_frame(image_size=size, logical_size=size)
        overlay = OverlayWindow(frame, monitor_geometries=monitor_geometries)
        overlay.set_selection(QRect(400, 200, 100, 100))
        overlay.show()
        QTest.qWaitForWindowExposed(overlay)
        return overlay

    def _pick_full_screen(self, overlay):
        QTest.mouseClick(overlay._bar._chip, Qt.MouseButton.LeftButton)
        QTest.mouseClick(
            overlay._popover._rows[self.FULL_SCREEN_LABEL], Qt.MouseButton.LeftButton
        )

    def test_selects_the_whole_window_on_a_single_monitor(self):
        overlay = self._overlay()

        self._pick_full_screen(overlay)

        assert overlay._selection == QRect(0, 0, 600, 600)

    def test_selects_the_monitor_the_cursor_last_moved_over(self):
        left = QRectF(0, 0, 250, 600)
        right = QRectF(250, 0, 350, 600)
        overlay = self._overlay(monitor_geometries=[left, right])
        # Throwaway move before the real one -- see the identical comment
        # on TestCaptureModeWindowIntegration._overlay; not folded into
        # this class's own _overlay() because the fallback test right
        # below needs `_cursor_pos` to still be None when it starts.
        QTest.mouseMove(overlay, QPoint(590, 590))
        QTest.mouseMove(overlay, QPoint(100, 100))  # inside `left`

        self._pick_full_screen(overlay)

        assert overlay._selection == left.toRect()

    def test_falls_back_to_the_windows_own_centre_with_no_prior_cursor_move(self):
        left = QRectF(0, 0, 250, 600)
        right = QRectF(250, 0, 350, 600)
        overlay = self._overlay(monitor_geometries=[left, right])
        # No QTest.mouseMove at all -- `_cursor_pos` is still None, so
        # the window's own centre (300, 300), inside `right` only, is
        # what decides the display.

        self._pick_full_screen(overlay)

        assert overlay._selection == right.toRect()

    def test_selection_from_full_screen_is_reframable_like_a_dragged_one(self):
        overlay = self._overlay()
        self._pick_full_screen(overlay)
        press = overlay._edge_handle_rect(Handle.BOTTOM).center().toPoint()
        # Comfortably clear of the `_BAR_ROOM` clamp (window height 600
        # minus 130 = 470) so the result is the plain dragged value, not
        # that clamp's own floor -- `_resize_selection`'s docstring is the
        # authority for why that clamp exists at all.
        target = QPoint(300, 300)

        QTest.mousePress(overlay, Qt.MouseButton.LeftButton, pos=press)
        QTest.mouseMove(overlay, target)
        QTest.mouseRelease(overlay, Qt.MouseButton.LeftButton, pos=target)

        assert overlay._selection == QRect(0, 0, 600, 300)

    def test_selection_from_full_screen_can_be_annotated(self):
        overlay = self._overlay()
        self._pick_full_screen(overlay)

        overlay.add_mark(_mark())

        assert len(overlay.marks) == 1


class TestCaptureModeFreeformIntegration:
    """SNX-49 AC: picking Freeform in the popover arms press-drag-release
    lasso tracing. Release confirms the traced path (closing it for the
    user if they didn't), sets `_selection` to its bounding box -- same as
    every other mode, so the selection frame/handles/chips/bar stay
    re-framable and annotatable exactly as before -- and stores the exact
    path as `_selection_path` for the scrim/export to key off separately.
    """

    @pytest.fixture(autouse=True)
    def _clean_slate(self):
        _close_stray_toplevel_windows()

    FREEFORM_LABEL = tokens.CAPTURE_MODES[3][0]

    # The same L-shape `TestFreeformMode` traces for `Overlay` above --
    # reused here so the excluded-notch assertions below rest on that
    # class's own already-proven path.contains() geometry rather than
    # re-derived coordinates.
    _STEM_TOP_LEFT = QPoint(20, 20)
    _STEM_TOP_RIGHT = QPoint(60, 20)
    _NOTCH_CORNER = QPoint(60, 80)
    _FOOT_TOP_RIGHT = QPoint(100, 80)
    _FOOT_BOTTOM_RIGHT = QPoint(100, 120)
    _FOOT_BOTTOM_LEFT = QPoint(20, 120)  # release point; never returns to the anchor

    def _overlay(self, size=(200, 200)):
        frame = make_frame(image_size=size, logical_size=size)
        overlay = OverlayWindow(frame)
        overlay.set_selection(QRect(0, 0, 50, 50))  # a prior selection to clear
        overlay.show()
        QTest.qWaitForWindowExposed(overlay)
        return overlay

    def _pick_freeform_mode(self, overlay):
        QTest.mouseClick(overlay._bar._chip, Qt.MouseButton.LeftButton)
        QTest.mouseClick(
            overlay._popover._rows[self.FREEFORM_LABEL], Qt.MouseButton.LeftButton
        )

    def _trace_l_shape(self, overlay):
        QTest.mousePress(overlay, Qt.MouseButton.LeftButton, pos=self._STEM_TOP_LEFT)
        QTest.mouseMove(overlay, self._STEM_TOP_RIGHT)
        QTest.mouseMove(overlay, self._NOTCH_CORNER)
        QTest.mouseMove(overlay, self._FOOT_TOP_RIGHT)
        QTest.mouseMove(overlay, self._FOOT_BOTTOM_RIGHT)
        QTest.mouseRelease(overlay, Qt.MouseButton.LeftButton, pos=self._FOOT_BOTTOM_LEFT)

    def test_picking_freeform_arms_picking_and_clears_the_prior_selection(self):
        overlay = self._overlay()

        self._pick_freeform_mode(overlay)

        assert overlay._picking_freeform
        assert overlay._selection is None

    def test_drag_traces_a_lasso_and_confirms_its_bounds_on_release(self):
        overlay = self._overlay()
        self._pick_freeform_mode(overlay)

        self._trace_l_shape(overlay)

        assert not overlay._picking_freeform
        assert overlay._selection == QRect(20, 20, 80, 100)
        assert isinstance(overlay._selection_path, QPainterPath)
        assert overlay._selection_path.contains(QPointF(30, 30))  # inside the stem
        assert overlay._selection_path.contains(QPointF(80, 100))  # inside the foot
        assert not overlay._selection_path.contains(QPointF(80, 40))  # in the notch

    def test_unclosed_lasso_is_closed_for_the_user_on_release(self):
        # The release point (_FOOT_BOTTOM_LEFT) never returns to the press
        # anchor (_STEM_TOP_LEFT) -- if the loop weren't closed for the
        # user, the region between the two along the L's own open edge
        # wouldn't be part of the filled path at all.
        overlay = self._overlay()
        self._pick_freeform_mode(overlay)

        self._trace_l_shape(overlay)

        assert overlay._selection_path.contains(QPointF(21, 100))

    def test_release_below_threshold_is_a_misfire_and_leaves_picking_armed(self):
        overlay = self._overlay()
        self._pick_freeform_mode(overlay)

        QTest.mouseClick(overlay, Qt.MouseButton.LeftButton, pos=QPoint(50, 50))

        assert overlay._picking_freeform
        assert overlay._selection is None

    def test_selection_from_freeform_can_be_annotated(self):
        overlay = self._overlay()
        self._pick_freeform_mode(overlay)
        self._trace_l_shape(overlay)

        overlay.add_mark(_mark())

        assert len(overlay.marks) == 1

    def test_resizing_a_freeform_selection_reverts_it_to_a_plain_rectangle(self):
        # The path was traced against the *original* bounding box; a
        # handle drag has no way to reshape it to match a dragged edge, so
        # re-framing silently drops the path rather than leaving a now-stale
        # one behind -- see `OverlayWindow.set_selection`'s own docstring.
        overlay = self._overlay()
        self._pick_freeform_mode(overlay)
        self._trace_l_shape(overlay)
        press = overlay._edge_handle_rect(Handle.RIGHT).center().toPoint()
        target = QPoint(150, press.y())

        QTest.mousePress(overlay, Qt.MouseButton.LeftButton, pos=press)
        QTest.mouseMove(overlay, target)
        QTest.mouseRelease(overlay, Qt.MouseButton.LeftButton, pos=target)

        assert overlay._selection_path is None


class _FakeCaptureBackend(CaptureBackend):
    """Unlike a `Mock`, this returns a real, distinguishable `Frame` --
    for proving a delayed re-capture's frame came from *this* backend
    rather than merely that `capture()` was called at all. Mirrors
    `test_app.py`'s own `FakeCaptureBackend`.
    """

    def __init__(self, frame: Frame):
        self._frame = frame

    def name(self) -> str:
        return "fake"

    def is_available(self) -> bool:
        return True

    def capture(self) -> Frame:
        return self._frame


class _FailingCaptureBackend(CaptureBackend):
    def name(self) -> str:
        return "failing"

    def is_available(self) -> bool:
        return True

    def capture(self) -> Frame:
        raise RuntimeError("capture failed")


class TestCaptureModeDelayIntegration:
    """SNX-50 AC: picking a mode in the popover while `_delay` isn't `Off`
    hides `OverlayWindow` before any wait begins, shows a countdown while
    it's gone, re-grabs through `_registry` -- the same `BackendRegistry`
    the first frame came through, per CLAUDE.md's one architectural rule
    applying to this grab exactly as it does to the first -- and re-opens
    over the fresh frame with the tool/colour/stroke the user had chosen.
    A delay of `Off` (the default `tokens.DELAYS[0]`) does none of that.
    """

    @pytest.fixture(autouse=True)
    def _clean_slate(self):
        _close_stray_toplevel_windows()

    REGION_LABEL = tokens.CAPTURE_MODES[0][0]
    FREEFORM_LABEL = tokens.CAPTURE_MODES[3][0]

    def _overlay(self, registry=None, size=(600, 600)):
        frame = make_frame(image_size=size, logical_size=size)
        overlay = OverlayWindow(frame, registry=registry)
        overlay.set_selection(QRect(400, 200, 100, 100))
        overlay.show()
        QTest.qWaitForWindowExposed(overlay)
        return overlay

    def _open_popover_and_set_delay(self, overlay, delay_clicks=1):
        QTest.mouseClick(overlay._bar._chip, Qt.MouseButton.LeftButton)
        for _ in range(delay_clicks):
            QTest.mouseClick(overlay._popover._delay_row, Qt.MouseButton.LeftButton)

    def _confirm_mode(self, overlay, label):
        QTest.mouseClick(overlay._popover._rows[label], Qt.MouseButton.LeftButton)

    def test_confirming_a_mode_with_a_delay_hides_the_overlay_before_the_wait_begins(self):
        overlay = self._overlay()
        self._open_popover_and_set_delay(overlay)  # DELAYS[1] == "3s"
        assert overlay._delay == tokens.DELAYS[1]

        self._confirm_mode(overlay, self.REGION_LABEL)

        assert not overlay.isVisible()
        # The wait itself hasn't elapsed at all yet -- this isn't "hidden
        # eventually," it's hidden synchronously, before the first tick.
        assert overlay._delay_remaining == 3

    def test_a_countdown_is_visible_while_the_overlay_is_hidden(self):
        overlay = self._overlay()
        self._open_popover_and_set_delay(overlay)

        self._confirm_mode(overlay, self.REGION_LABEL)

        assert not overlay.isVisible()
        assert overlay._countdown.isVisible()
        assert overlay._countdown._label.text() == "3"

    def test_countdown_ticks_down_once_per_second_elapsed(self):
        overlay = self._overlay()
        self._open_popover_and_set_delay(overlay)
        self._confirm_mode(overlay, self.REGION_LABEL)

        overlay._delay_timer.timeout.emit()
        assert overlay._countdown._label.text() == "2"
        overlay._delay_timer.timeout.emit()
        assert overlay._countdown._label.text() == "1"

    def test_after_the_wait_a_fresh_frame_is_captured_through_the_registry(self):
        original_frame = make_frame(image_size=(600, 600), logical_size=(600, 600))
        regrabbed_frame = make_frame(image_size=(600, 600), logical_size=(600, 600))
        registry = BackendRegistry([_FakeCaptureBackend(regrabbed_frame)])
        overlay = OverlayWindow(original_frame, registry=registry)
        overlay.set_selection(QRect(400, 200, 100, 100))
        overlay.show()
        QTest.qWaitForWindowExposed(overlay)
        self._open_popover_and_set_delay(overlay)
        self._confirm_mode(overlay, self.REGION_LABEL)

        for _ in range(3):
            overlay._delay_timer.timeout.emit()

        # Identity, not just equal content: this must be the frame the
        # fake backend handed back through `_registry.capture()`, never
        # the original `frame` the constructor was given.
        assert overlay._frame is regrabbed_frame
        assert overlay._frame is not original_frame

    def test_the_overlay_reopens_once_the_countdown_reaches_zero(self):
        regrabbed_frame = make_frame(image_size=(600, 600), logical_size=(600, 600))
        registry = BackendRegistry([_FakeCaptureBackend(regrabbed_frame)])
        overlay = self._overlay(registry=registry)
        self._open_popover_and_set_delay(overlay)
        self._confirm_mode(overlay, self.REGION_LABEL)

        for _ in range(3):
            overlay._delay_timer.timeout.emit()

        assert overlay.isVisible()
        assert not overlay._countdown.isVisible()
        # The stale selection described the *old* content; it's cleared
        # rather than carried over onto the new frame's pixels.
        assert overlay._selection is None

    def test_reopened_overlay_keeps_the_tool_colour_and_stroke_the_user_chose(self):
        regrabbed_frame = make_frame(image_size=(600, 600), logical_size=(600, 600))
        registry = BackendRegistry([_FakeCaptureBackend(regrabbed_frame)])
        overlay = self._overlay(registry=registry)
        QTest.mouseClick(overlay._bar._tool_buttons["pen"], Qt.MouseButton.LeftButton)
        _name, target_hex = tokens.INK_SWATCHES[3]
        QTest.mouseClick(overlay._tray._swatch_buttons[target_hex], Qt.MouseButton.LeftButton)
        overlay._tray._slider.setValue(21)
        self._open_popover_and_set_delay(overlay)
        self._confirm_mode(overlay, self.REGION_LABEL)

        for _ in range(3):
            overlay._delay_timer.timeout.emit()

        assert overlay._bar.active_tool == "pen"
        assert overlay._ink_colour == target_hex
        assert overlay._stroke_width == 21

    def test_delayed_freeform_pick_still_arms_lasso_tracing_on_the_new_frame(self):
        # The picked mode isn't forgotten across the wait -- Window/Full
        # screen/Freeform still do their own thing against the fresh
        # frame, same as they would with no delay at all.
        regrabbed_frame = make_frame(image_size=(600, 600), logical_size=(600, 600))
        registry = BackendRegistry([_FakeCaptureBackend(regrabbed_frame)])
        overlay = self._overlay(registry=registry)
        self._open_popover_and_set_delay(overlay)
        self._confirm_mode(overlay, self.FREEFORM_LABEL)

        for _ in range(3):
            overlay._delay_timer.timeout.emit()

        assert overlay._picking_freeform

    def test_failed_regrab_restores_the_old_frame_and_toasts_instead_of_crashing(self):
        original_frame = make_frame(image_size=(600, 600), logical_size=(600, 600))
        registry = BackendRegistry([_FailingCaptureBackend()])
        overlay = OverlayWindow(original_frame, registry=registry)
        overlay.set_selection(QRect(400, 200, 100, 100))
        overlay.show()
        QTest.qWaitForWindowExposed(overlay)
        self._open_popover_and_set_delay(overlay)
        self._confirm_mode(overlay, self.REGION_LABEL)

        for _ in range(3):
            overlay._delay_timer.timeout.emit()

        assert overlay.isVisible()
        assert overlay._frame is original_frame
        assert overlay._toast.isVisible()
        assert "capture failed" in overlay._toast._text_label.text()

    def test_delay_off_captures_immediately_with_no_hide_or_countdown(self):
        overlay = self._overlay()
        assert overlay._delay == tokens.DELAYS[0]  # "No delay", the default

        QTest.mouseClick(overlay._bar._chip, Qt.MouseButton.LeftButton)
        self._confirm_mode(overlay, self.FREEFORM_LABEL)

        assert overlay.isVisible()
        assert overlay._countdown is None
        # Freeform's own immediate arming (SNX-49) still ran -- proving this
        # went through the ordinary dispatch path, not a delay that just
        # happened to finish instantly.
        assert overlay._picking_freeform


class TestFreeformExport:
    """SNX-49 AC: the exported image is cropped to the lasso's bounding box
    with the pixels outside the path fully transparent, in a format
    (`QImage`, later written as PNG by `app.save_image`) that preserves
    that transparency.
    """

    def _overlay_with_triangular_lasso(self):
        # A right triangle within a square bounding box: (10,10)-(10,60) up
        # the left edge, (10,60)-(60,60) along the bottom, and the
        # hypotenuse (60,60)-(10,10) closing it -- so a point near the
        # bounding box's excluded top-right corner is unambiguously outside
        # the path while its own centre is unambiguously inside.
        frame = make_frame(image_size=(200, 200), logical_size=(200, 200))
        overlay = OverlayWindow(frame)
        path = QPainterPath()
        path.moveTo(QPointF(10, 10))
        path.lineTo(QPointF(10, 60))
        path.lineTo(QPointF(60, 60))
        path.closeSubpath()
        overlay.set_selection(QRect(10, 10, 50, 50), path=path)
        return overlay

    def test_pixels_outside_the_path_are_fully_transparent(self):
        overlay = self._overlay_with_triangular_lasso()

        rendered = overlay.rendered_image()

        # `rendered` is already cropped to the selection's own (10,10)
        # top-left, so these are that crop's local pixel coordinates --
        # (15,55)/(45,15) in the pre-crop, window-local space the path
        # itself is defined in, shifted back by the selection's origin.
        assert pixel(rendered, 5, 45).alpha() == 255  # inside the triangle
        assert pixel(rendered, 35, 5).alpha() == 0  # excluded corner

    def test_cropped_to_the_bounding_box_size(self):
        overlay = self._overlay_with_triangular_lasso()

        rendered = overlay.rendered_image()

        assert rendered.size() == QSize(50, 50)

    def test_non_freeform_selections_stay_fully_opaque(self):
        # No regression: a plain rectangular selection (no `_selection_path`)
        # must not suddenly pick up an alpha channel it never had before.
        frame = make_frame(image_size=(200, 200), logical_size=(200, 200))
        overlay = OverlayWindow(frame)
        overlay.set_selection(QRect(10, 10, 50, 50))

        rendered = overlay.rendered_image()

        assert pixel(rendered, 15, 15).alpha() == 255

    def test_saved_png_preserves_the_transparency(self, monkeypatch, tmp_path):
        monkeypatch.setattr(app_module.Path, "home", lambda: tmp_path)
        overlay = self._overlay_with_triangular_lasso()

        path = overlay.save()

        saved = QImage(str(path))
        assert pixel(saved, 5, 45).alpha() == 255
        assert pixel(saved, 35, 5).alpha() == 0


def _mark(start=(10, 10), end=(20, 20)):
    return Rectangle(
        colour=QColor(255, 0, 0), stroke_width=4, start=QPointF(*start), end=QPointF(*end)
    )


class TestDimensionChipText:
    """SNX-43: the dimension chip's text -- the selection's *logical* size,
    and the mark-count pluralisation rule ("1 mark" singular, "N marks"
    otherwise) -- per docs/design/overlay-redesign.md's "Chips above the
    selection".
    """

    def _overlay(self, size=(600, 600)):
        frame = make_frame(image_size=size, logical_size=size)
        overlay = OverlayWindow(frame)
        overlay.set_selection(QRect(100, 100, 300, 150))
        return overlay

    def test_reads_the_selection_size(self):
        overlay = self._overlay()

        size_text, _ = overlay._dimension_chip_texts()

        assert size_text == "300 × 150"

    def test_reads_logical_size_not_physical_pixels_under_scaling(self):
        # The image is 2x the logical size -- a devicePixelRatio-2 display
        # -- so a read of the frame's own pixel geometry (rather than the
        # window-local logical `_selection` this window already keeps, per
        # the class docstring) would double every number here.
        frame = make_frame(image_size=(1200, 800), logical_size=(600, 400))
        overlay = OverlayWindow(frame)
        overlay.set_selection(QRect(50, 50, 200, 120))

        size_text, _ = overlay._dimension_chip_texts()

        assert size_text == "200 × 120"

    def test_zero_marks_reads_plural(self):
        overlay = self._overlay()

        _, mark_text = overlay._dimension_chip_texts()

        assert mark_text == "0 marks"

    def test_exactly_one_mark_reads_singular(self):
        overlay = self._overlay()
        overlay.add_mark(_mark())

        _, mark_text = overlay._dimension_chip_texts()

        assert mark_text == "1 mark"

    def test_two_marks_reads_plural(self):
        overlay = self._overlay()
        overlay.add_mark(_mark())
        overlay.add_mark(_mark())

        _, mark_text = overlay._dimension_chip_texts()

        assert mark_text == "2 marks"

    def test_undoing_back_to_one_mark_returns_to_singular(self):
        # The count is read fresh every call (see the method's own
        # docstring), so it has to follow the mark count back down, not
        # just up.
        overlay = self._overlay()
        overlay.add_mark(_mark())
        overlay.add_mark(_mark())

        overlay.undo()

        _, mark_text = overlay._dimension_chip_texts()
        assert mark_text == "1 mark"


class TestDimensionChipLiveUpdate:
    """SNX-43 AC: 'the chip updates live while the selection is being
    resized.'
    """

    def test_text_reflects_the_in_progress_drag_before_release(self):
        frame = make_frame(image_size=(500, 500), logical_size=(500, 500))
        overlay = OverlayWindow(frame)
        overlay.set_selection(QRect(100, 100, 150, 100))
        before, _ = overlay._dimension_chip_texts()
        assert before == "150 × 100"
        press = overlay._edge_handle_rect(Handle.RIGHT).center().toPoint()

        QTest.mousePress(overlay, Qt.MouseButton.LeftButton, pos=press)
        QTest.mouseMove(overlay, QPoint(300, 150))  # still mid-drag, no release yet

        mid_drag, _ = overlay._dimension_chip_texts()
        assert mid_drag == "200 × 100"

        QTest.mouseRelease(overlay, Qt.MouseButton.LeftButton, pos=QPoint(300, 150))


class TestDimensionChipGeometry:
    """SNX-43 AC: 'the dimension chip is left-aligned to the selection's
    left edge... above the selection's top edge.'
    """

    def _overlay(self, size=(600, 600)):
        frame = make_frame(image_size=size, logical_size=size)
        overlay = OverlayWindow(frame)
        overlay.set_selection(QRect(120, 200, 260, 140))
        return overlay

    def test_left_aligned_to_the_selections_left_edge(self):
        overlay = self._overlay()

        rect = overlay._dimension_chip_rect()

        assert rect.left() == pytest.approx(overlay._selection.left())

    def test_sits_the_token_offset_above_the_selections_top_edge(self):
        overlay = self._overlay()

        rect = overlay._dimension_chip_rect()

        assert rect.top() == pytest.approx(
            overlay._selection.top() - tokens.Metric.CHIP_OFFSET_Y
        )

    def test_grows_to_fit_a_longer_mark_count_reading(self):
        overlay = self._overlay()
        narrow = overlay._dimension_chip_rect()
        for _ in range(20):
            overlay.add_mark(_mark())

        wide = overlay._dimension_chip_rect()

        assert wide.width() > narrow.width()
        # Still left-aligned to the same edge -- only the right edge grows.
        assert wide.left() == pytest.approx(narrow.left())


class TestFrozenPillGeometry:
    """SNX-43 AC: 'the Frozen pill is right-aligned to its right edge...
    above the selection's top edge.'
    """

    def _overlay(self, size=(600, 600)):
        frame = make_frame(image_size=size, logical_size=size)
        overlay = OverlayWindow(frame)
        overlay.set_selection(QRect(120, 200, 260, 140))
        return overlay

    def test_right_aligned_to_the_selections_right_edge(self):
        overlay = self._overlay()
        sel = QRectF(overlay._selection)  # QRect.right() is inclusive; QRectF's isn't

        rect = overlay._frozen_pill_rect()

        assert rect.right() == pytest.approx(sel.right())

    def test_sits_the_token_offset_above_the_selections_top_edge(self):
        overlay = self._overlay()

        rect = overlay._frozen_pill_rect()

        assert rect.top() == pytest.approx(
            overlay._selection.top() - tokens.Metric.CHIP_OFFSET_Y
        )

    def test_sits_at_the_same_height_as_the_dimension_chip(self):
        overlay = self._overlay()

        assert overlay._frozen_pill_rect().top() == pytest.approx(
            overlay._dimension_chip_rect().top()
        )


class TestChipsPixels:
    """SNX-43: both chips actually paint their token-coloured fill."""

    def _overlay(self, size=(900, 900)):
        frame = make_frame(image_size=size, logical_size=size)
        overlay = OverlayWindow(frame)
        # Wide enough that the two chips -- one left-aligned to the
        # selection, one right-aligned -- never overlap regardless of which
        # mono/sans family design.font_families() falls back to when IBM
        # Plex isn't bundled (a wider fallback glyph could otherwise widen
        # the dimension chip enough to reach under the Frozen pill's own
        # sample point).
        overlay.set_selection(QRect(150, 200, 500, 120))
        return overlay

    def test_dimension_chip_paints_its_light_background(self):
        overlay = self._overlay()
        sample = overlay._dimension_chip_rect().center().toPoint()

        rendered = overlay.grab().toImage()

        assert pixel(rendered, sample) == design_color("CHIP_LIGHT_BG")

    def test_frozen_pill_paints_its_dark_background_at_the_token_alpha(self):
        overlay = self._overlay()
        rect = overlay._frozen_pill_rect()
        # Near the right edge, past where the pin icon/label are painted, so
        # this samples the plain fill rather than a glyph pixel.
        sample = QPoint(round(rect.right() - 3), round(rect.center().y()))

        rendered = overlay.grab().toImage()
        # The pill sits above the selection, i.e. over the *scrim*, not the
        # bare frame -- so the base this blends onto is itself already
        # DIM-blended, same as the scrim's own token colour/alpha.
        scrimmed = _blend(QColor(10, 20, 30), design_color("DIM"))
        expected = _blend(scrimmed, design_color("CHIP_DARK_BG"))
        sampled = pixel(rendered, sample)

        assert sampled.red() == pytest.approx(expected.red(), abs=2)
        assert sampled.green() == pytest.approx(expected.green(), abs=2)
        assert sampled.blue() == pytest.approx(expected.blue(), abs=2)


class TestChipsExcludedFromExport:
    """SNX-43 AC: 'neither chip appears in the exported image.'"""

    def _overlay(self, size=(600, 600)):
        frame = make_frame(image_size=size, logical_size=size)
        overlay = OverlayWindow(frame)
        overlay.set_selection(QRect(150, 200, 200, 120))
        return overlay

    def test_rendered_image_size_matches_the_selection_not_the_chips(self):
        overlay = self._overlay()

        rendered = overlay.rendered_image()

        # Both chips sit outside the selection rect (above its top edge);
        # if either had leaked into the export, the flattened image would
        # be taller than the plain selection instead of exactly its size.
        assert (rendered.width(), rendered.height()) == (200, 120)

    def test_rendered_image_pixels_match_the_frames_own_colour(self):
        overlay = self._overlay()

        rendered = overlay.rendered_image()

        base = QColor(10, 20, 30)
        for x, y in [(0, 0), (199, 0), (0, 119), (199, 119), (100, 60)]:
            assert pixel(rendered, x, y) == base


class TestHintHUDComposition:
    """SNX-46: the standalone `HintHUD` widget's content -- the exact hint
    line from docs/design/overlay-redesign.md's "Top hint HUD" section, with
    key names (`Esc`, `Enter`, the eight tool shortcuts) set apart from the
    surrounding prose by family and colour, per "Key names are mono in pure
    white."
    """

    def test_reads_the_full_hint_line(self):
        hud = HintHUD()

        text = "".join(label.text() for label in hud.findChildren(QLabel))

        assert text == (
            "Esc discard ink · Enter copy & dismiss · "
            "P H A R S T B E pick a tool · drag any edge to re-frame "
            "— the ink stays where you put it"
        )

    def test_key_segments_cover_esc_enter_and_every_tool_shortcut_in_order(self):
        hud = HintHUD()

        key_texts = [text for text, is_key in hud._segments() if is_key]

        assert key_texts == [
            "Esc",
            "Enter",
            " ".join(_TOOL_SHORTCUT_KEYS[tool] for tool in tokens.TOOLS),
        ]

    def test_key_segments_are_set_in_the_mono_family_at_pure_white(self):
        hud = HintHUD()
        labels = {label.text(): label for label in hud.findChildren(QLabel)}
        mono = font_families().mono
        key_colour = design_color("HUD_KEY").name()

        for text, is_key in hud._segments():
            if is_key:
                label = labels[text]
                assert label.font().family() == mono
                assert label.styleSheet() == f"color: {key_colour};"

    def test_prose_segments_are_set_in_the_ui_family_at_the_muted_hud_colour(self):
        hud = HintHUD()
        labels = {label.text(): label for label in hud.findChildren(QLabel)}
        ui = font_families().ui
        prose_colour = design_color("HUD_TEXT").name()

        for text, is_key in hud._segments():
            if not is_key:
                label = labels[text]
                assert label.font().family() == ui
                assert label.styleSheet() == f"color: {prose_colour};"

    def test_key_colour_reads_brighter_than_the_prose_colour(self):
        # "Key names are mono in pure white" against the surrounding
        # prose's own muted colour -- checked as a plain luminance
        # comparison rather than hard-coding "white", so this stays true
        # even if HUD_KEY/HUD_TEXT's exact hexes ever change.
        key = design_color("HUD_KEY")
        prose = design_color("HUD_TEXT")

        assert key.lightness() > prose.lightness()

    def test_height_matches_the_token(self):
        hud = HintHUD()

        assert hud.height() == tokens.Metric.HUD_H


class TestHintHUDFill:
    """SNX-46: the bar's own translucent fill -- `HUD_BG` at
    `HUD_BG_ALPHA` -- same convention as `TestFloatingBarFill`'s callout for
    the floating bar's own "alpha, not opacity" fill.
    """

    def test_background_pixel_is_painted_at_the_token_alpha(self):
        hud = HintHUD()
        # Wider than the hint line's own sizeHint, so the stretches on
        # either side of the centred text leave real background-only room
        # to sample near an edge.
        hud.resize(hud.sizeHint().width() + 400, tokens.Metric.HUD_H)

        rendered = hud.grab().toImage()
        sampled = pixel(rendered, 2, 2)

        expected_alpha = round(tokens.Color.HUD_BG_ALPHA * 255)
        expected_rgb = QColor(tokens.Color.HUD_BG).getRgb()[:3]
        assert sampled.alpha() == pytest.approx(expected_alpha, abs=2)
        # abs=1, not exact equality: a 50%-alpha fill's premultiplied RGB
        # can round either way, the same one-off drift TestOverlayWindow's
        # own DIM-scrim test (_blend) already tolerates for the same reason.
        for sampled, expected in zip(
            (sampled.red(), sampled.green(), sampled.blue()), expected_rgb
        ):
            assert sampled == pytest.approx(expected, abs=1)


class TestHintHUDOverlayIntegration:
    """SNX-46: `HintHUD` wired into `OverlayWindow` as `_hud`, behind the
    `hints` preference the spec's "Top hint HUD" section puts it behind --
    default off as of SNX-65, since the banner read as a stray element
    across the top of every capture rather than help -- and gated on this
    window's own visibility the same way `_bar`/`_toast` already are
    (`TestFloatingBarIntegration`/`TestOverlayWindowToasts` above document
    why).
    """

    def _overlay(self, size=(800, 600), **kwargs):
        frame = make_frame(image_size=size, logical_size=size)
        return OverlayWindow(frame, **kwargs)

    def test_hints_enabled_defaults_to_false(self):
        overlay = self._overlay()

        assert not overlay.hints_enabled

    def test_hud_becomes_visible_once_the_overlay_is_shown(self):
        overlay = self._overlay(hints_enabled=True)

        overlay.show()
        QTest.qWaitForWindowExposed(overlay)

        assert overlay._hud.isVisible()

    def test_hud_spans_the_full_window_width_at_the_token_height(self):
        overlay = self._overlay(size=(800, 600))

        assert overlay._hud.geometry() == QRect(0, 0, 800, tokens.Metric.HUD_H)

    def test_hud_stays_hidden_while_the_overlay_itself_is_not_shown(self):
        # Mirrors
        # test_bar_stays_hidden_and_unpainted_while_the_overlay_itself_is_not_shown:
        # none of this file's other OverlayWindow pixel tests call .show()
        # before grab()ing, so the HUD must not leak into any of them just
        # because it exists as a real child widget -- true regardless of the
        # preference, but hints_enabled=True is the stricter case since
        # default-off alone would already keep it hidden here.
        overlay = self._overlay(hints_enabled=True)

        assert not overlay._hud.isVisible()

    def test_set_hints_enabled_false_hides_the_hud_immediately(self):
        overlay = self._overlay(hints_enabled=True)
        overlay.show()
        QTest.qWaitForWindowExposed(overlay)
        assert overlay._hud.isVisible()

        overlay.set_hints_enabled(False)

        assert not overlay._hud.isVisible()
        assert not overlay.hints_enabled

    def test_set_hints_enabled_true_shows_it_again(self):
        overlay = self._overlay(hints_enabled=True)
        overlay.show()
        QTest.qWaitForWindowExposed(overlay)
        overlay.set_hints_enabled(False)
        assert not overlay._hud.isVisible()

        overlay.set_hints_enabled(True)

        assert overlay._hud.isVisible()

    def test_constructor_can_start_with_the_preference_off(self):
        overlay = self._overlay(hints_enabled=False)
        overlay.show()
        QTest.qWaitForWindowExposed(overlay)

        assert not overlay.hints_enabled
        assert not overlay._hud.isVisible()

    def test_constructor_can_start_with_the_preference_on(self):
        # The AC's "still reachable... for a user who wants to see the
        # shortcuts": a caller (or `?`, see TestHintPreferenceKeyboardToggle
        # below) can still ask for the banner from the very first frame.
        overlay = self._overlay(hints_enabled=True)
        overlay.show()
        QTest.qWaitForWindowExposed(overlay)

        assert overlay.hints_enabled
        assert overlay._hud.isVisible()

    def test_hud_hides_again_once_the_overlay_itself_is_hidden(self):
        overlay = self._overlay(hints_enabled=True)
        overlay.show()
        QTest.qWaitForWindowExposed(overlay)
        assert overlay._hud.isVisible()

        overlay.hide()

        assert not overlay._hud.isVisible()


class TestReframingClearsTheShownHUD:
    """SNX-46 AC: 'the selection cannot be dragged up underneath the HUD
    while it is shown.' SNX-33's own `_TOP_CLEARANCE` (52) already reserved
    this room ahead of the HUD existing -- this asserts the two actually
    agree, per the ticket's "the HUD and that constraint have to agree,"
    rather than just trusting the arithmetic in each one's comments.

    SNX-65: hints are off by default now, so every case here that means to
    exercise the clamp constructs with `hints_enabled=True` explicitly --
    the class name says "shown" HUD, not "possibly shown."
    """

    def test_top_clearance_constant_is_at_least_the_huds_own_height(self):
        assert OverlayWindow._TOP_CLEARANCE >= tokens.Metric.HUD_H

    def test_dragging_the_top_left_corner_off_screen_stops_clear_of_the_visible_hud(self):
        frame = make_frame(image_size=(400, 400), logical_size=(400, 400))
        overlay = OverlayWindow(frame, hints_enabled=True)
        overlay.show()
        QTest.qWaitForWindowExposed(overlay)
        assert overlay._hud.isVisible()
        overlay.set_selection(QRect(100, 100, 150, 100))
        press = overlay._corner_hit_rect(Handle.TOP_LEFT).center().toPoint()

        QTest.mousePress(overlay, Qt.MouseButton.LeftButton, pos=press)
        QTest.mouseMove(overlay, QPoint(-50, -50))
        QTest.mouseRelease(overlay, Qt.MouseButton.LeftButton, pos=QPoint(-50, -50))

        # QRect.bottom() is inclusive (top + height - 1) -- the same trap
        # `_bracket_path`/`FloatingBarIntegration` already document -- so
        # the clamped selection's top must clear it, not just equal it.
        assert overlay._selection.y() > overlay._hud.geometry().bottom()

    def test_dragging_the_top_left_corner_off_screen_reaches_the_top_with_hints_off(self):
        # SNX-65 AC: "the selection can use the screen space the HUD
        # previously reserved at the top." With hints off (the default),
        # `_TOP_CLEARANCE` must not hold that 52px strip shut for a bar
        # nobody is shown -- the corner should be free to reach y == 0.
        frame = make_frame(image_size=(400, 400), logical_size=(400, 400))
        overlay = OverlayWindow(frame)
        overlay.show()
        QTest.qWaitForWindowExposed(overlay)
        assert not overlay._hud.isVisible()
        overlay.set_selection(QRect(100, 100, 150, 100))
        press = overlay._corner_hit_rect(Handle.TOP_LEFT).center().toPoint()

        QTest.mousePress(overlay, Qt.MouseButton.LeftButton, pos=press)
        QTest.mouseMove(overlay, QPoint(-50, -50))
        QTest.mouseRelease(overlay, Qt.MouseButton.LeftButton, pos=QPoint(-50, -50))

        assert overlay._selection.y() == 0


class TestHintHUDExcludedFromExport:
    """SNX-46 AC: 'the HUD never appears in the exported image.'"""

    def test_rendered_image_is_identical_whether_or_not_the_hud_is_shown(self):
        size = (400, 400)
        frame = make_frame(image_size=size, logical_size=size)
        overlay = OverlayWindow(frame, hints_enabled=True)
        overlay.set_selection(QRect(52, 52, 200, 150))
        overlay.show()
        QTest.qWaitForWindowExposed(overlay)
        assert overlay._hud.isVisible()  # actually on screen, not a no-op

        with_hud = overlay.rendered_image()

        overlay.set_hints_enabled(False)
        assert not overlay._hud.isVisible()

        without_hud = overlay.rendered_image()

        assert with_hud == without_hud


class TestHintPreferenceKeyboardToggle:
    """SNX-65 AC: the hint text stays reachable, without editing a file, for
    a user who wants to see the shortcuts -- `?` flips `_hints_enabled` the
    same way a caller driving `set_hints_enabled` directly would.
    """

    def _overlay(self, **kwargs):
        frame = make_frame(image_size=(200, 200), logical_size=(200, 200))
        overlay = OverlayWindow(frame, **kwargs)
        overlay.set_selection(QRect(0, 0, 200, 200))
        overlay.show()
        QTest.qWaitForWindowExposed(overlay)
        return overlay

    def test_question_mark_turns_the_hud_on_from_the_default_off_state(self):
        overlay = self._overlay()
        assert not overlay._hud.isVisible()

        QTest.keyClick(overlay, Qt.Key.Key_Question)

        assert overlay.hints_enabled
        assert overlay._hud.isVisible()

    def test_question_mark_turns_the_hud_back_off(self):
        overlay = self._overlay(hints_enabled=True)
        assert overlay._hud.isVisible()

        QTest.keyClick(overlay, Qt.Key.Key_Question)

        assert not overlay.hints_enabled
        assert not overlay._hud.isVisible()

    def test_question_mark_is_suppressed_while_shortcuts_are_suppressed(self):
        # Mirrors the tool-letter shortcuts' own suppression while a
        # text-editing widget has focus (_shortcuts_suppressed) -- typing
        # "?" into a label must not also toggle the HUD out from under it.
        # Not shown, same as TestKeyboardToolShortcuts's own suppression
        # cases -- self.focusWidget() reports a child given focus via
        # setFocus() regardless of whether the window is ever shown, and an
        # actually-shown window here would hand focus to the bar's first
        # button instead of respecting this label's setFocus() call.
        frame = make_frame(image_size=(200, 200), logical_size=(200, 200))
        overlay = OverlayWindow(frame)
        overlay.set_selection(QRect(0, 0, 200, 200))
        label = QLineEdit(overlay)
        label.setFocus()

        QTest.keyClick(overlay, Qt.Key.Key_Question)

        assert not overlay.hints_enabled


class TestKeyboardToolShortcuts:
    """SNX-47 AC: 'each letter in tokens.SHORTCUTS selects its tool.'"""

    RED = QColor(255, 0, 0)

    def _overlay(self):
        frame = make_frame(image_size=(200, 200), logical_size=(200, 200))
        overlay = OverlayWindow(frame)
        overlay.set_selection(QRect(0, 0, 200, 200))
        return overlay

    @pytest.mark.parametrize("letter,tool", list(tokens.SHORTCUTS.items()))
    def test_letter_selects_its_tool(self, letter, tool):
        overlay = self._overlay()
        key = getattr(Qt.Key, f"Key_{letter}")

        QTest.keyClick(overlay, key)

        assert overlay._bar.active_tool == tool

    def test_selecting_eraser_by_key_arms_it_same_as_a_click(self):
        # Mirrors test_clicking_the_eraser_tool_button_arms_the_eraser --
        # a shortcut has to produce the same _on_tool_selected side effect
        # a button click does, not just move the active-tool highlight.
        overlay = self._overlay()

        QTest.keyClick(overlay, Qt.Key.Key_E)

        assert overlay._eraser_active

    def test_switching_tool_by_key_disarms_the_eraser(self):
        overlay = self._overlay()
        QTest.keyClick(overlay, Qt.Key.Key_E)
        assert overlay._eraser_active

        QTest.keyClick(overlay, Qt.Key.Key_P)

        assert not overlay._eraser_active
        assert overlay._bar.active_tool == "pen"


class TestKeyboardUndoRedo:
    """SNX-47 AC: 'Ctrl+Z undoes and Ctrl+Shift+Z redoes.'"""

    RED = QColor(255, 0, 0)

    def _overlay(self):
        frame = make_frame(image_size=(200, 200), logical_size=(200, 200))
        overlay = OverlayWindow(frame)
        overlay.set_selection(QRect(0, 0, 200, 200))
        overlay.add_mark(
            Rectangle(colour=self.RED, stroke_width=4, start=QPointF(0, 0), end=QPointF(10, 10))
        )
        return overlay

    def test_ctrl_z_undoes_the_newest_mark(self):
        overlay = self._overlay()

        QTest.keyClick(overlay, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)

        assert overlay.marks == ()
        assert overlay.can_redo

    def test_ctrl_shift_z_redoes_the_undone_mark(self):
        overlay = self._overlay()
        mark = overlay.marks[0]
        QTest.keyClick(overlay, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
        assert overlay.marks == ()

        QTest.keyClick(
            overlay,
            Qt.Key.Key_Z,
            Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier,
        )

        assert overlay.marks == (mark,)


class TestKeyboardEnter:
    """SNX-47 AC: 'Enter copies to the clipboard and dismisses the
    overlay.'
    """

    RED = QColor(255, 0, 0)

    def _overlay(self, with_selection=True):
        frame = make_frame(image_size=(200, 200), logical_size=(200, 200))
        overlay = OverlayWindow(frame)
        if with_selection:
            overlay.set_selection(QRect(0, 0, 200, 200))
        overlay.show()
        QTest.qWaitForWindowExposed(overlay)
        return overlay

    def test_enter_copies_the_flattened_selection_and_closes(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            app_module, "copy_image_to_clipboard", lambda image: calls.append(image)
        )
        overlay = self._overlay()
        overlay.add_mark(
            Rectangle(colour=self.RED, stroke_width=6, start=QPointF(20, 20), end=QPointF(80, 80))
        )

        QTest.keyClick(overlay, Qt.Key.Key_Return)

        assert len(calls) == 1
        assert calls[0].pixelColor(20, 50) == self.RED
        assert not overlay.isVisible()

    def test_enter_key_variant_also_dismisses(self, monkeypatch):
        monkeypatch.setattr(app_module, "copy_image_to_clipboard", lambda image: None)
        overlay = self._overlay()

        QTest.keyClick(overlay, Qt.Key.Key_Enter)

        assert not overlay.isVisible()

    def test_enter_without_a_selection_does_nothing(self, monkeypatch):
        # Mirrors Overlay's own Enter guard above -- nothing to flatten or
        # copy without a selection yet.
        calls = []
        monkeypatch.setattr(
            app_module, "copy_image_to_clipboard", lambda image: calls.append(image)
        )
        overlay = self._overlay(with_selection=False)

        QTest.keyClick(overlay, Qt.Key.Key_Return)

        assert calls == []
        assert overlay.isVisible()


class TestLabelEnterDoesNotDismissOverlay:
    """SNX-76: Return committed a focused label as a Text mark *and* went on
    to fire OverlayWindow.keyPressEvent's own Enter shortcut in the same
    keystroke, copying and closing the overlay the user only meant to add
    one label to. Stock QLineEdit deliberately leaves Return unaccepted
    after emitting editingFinished (so a dialog's default button can still
    fire from inside a focused field), and Qt walks an unaccepted key event
    up the parent-widget chain -- `_commit_text` had already hidden the
    label and dropped its focus by the time that second delivery reached
    `_shortcuts_suppressed()`, so the guard no longer saw an editor there to
    suppress for.

    Every test below delivers the key to `overlay._text_edit` itself (or,
    for Escape, to `overlay` -- QLineEdit never claims Escape, so it always
    reached OverlayWindow.keyPressEvent the ordinary way) the way a real
    keystroke would, rather than calling `overlay.keyPressEvent` directly --
    that would skip the very propagation this bug depended on and pass
    either way.
    """

    RED = QColor(255, 0, 0)

    def _overlay(self):
        frame = make_frame(image_size=(200, 200), logical_size=(200, 200))
        overlay = OverlayWindow(frame)
        overlay.set_selection(QRect(0, 0, 200, 200))
        overlay.show()
        QTest.qWaitForWindowExposed(overlay)
        return overlay

    def test_enter_commits_the_label_and_leaves_the_overlay_open(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            app_module, "copy_image_to_clipboard", lambda image: calls.append(image)
        )
        overlay = self._overlay()
        overlay._bar.select_tool("text")
        QTest.mousePress(overlay, Qt.MouseButton.LeftButton, pos=QPoint(30, 30))
        QApplication.processEvents()

        QTest.keyClicks(overlay._text_edit, "hello")
        QTest.keyClick(overlay._text_edit, Qt.Key.Key_Return)

        assert len(overlay.marks) == 1
        assert isinstance(overlay.marks[0], Text)
        assert overlay.marks[0].text == "hello"
        assert overlay._text_edit.isHidden()
        assert overlay.isVisible()  # never copied-and-dismissed
        assert calls == []

    def test_escape_abandons_the_label_without_touching_other_marks(self):
        overlay = self._overlay()
        overlay.add_mark(
            Rectangle(colour=self.RED, stroke_width=4, start=QPointF(0, 0), end=QPointF(10, 10))
        )
        overlay._bar.select_tool("text")
        QTest.mousePress(overlay, Qt.MouseButton.LeftButton, pos=QPoint(30, 30))
        QApplication.processEvents()
        QTest.keyClicks(overlay._text_edit, "hello")

        QTest.keyClick(overlay, Qt.Key.Key_Escape)

        assert overlay._text_edit.isHidden()
        assert len(overlay.marks) == 1
        assert isinstance(overlay.marks[0], Rectangle)  # the earlier mark survives
        assert overlay.isVisible()

    def test_enter_with_no_label_being_edited_still_copies_and_closes(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            app_module, "copy_image_to_clipboard", lambda image: calls.append(image)
        )
        overlay = self._overlay()

        QTest.keyClick(overlay, Qt.Key.Key_Return)

        assert len(calls) == 1
        assert not overlay.isVisible()


class TestKeyboardEscapeTwoStage:
    """SNX-47 AC: 'Esc with marks present discards them and toasts, and Esc
    with no marks present closes the overlay without capturing.' The
    two-stage split itself is the decision docs/design/overlay-redesign.md's
    keyboard table leaves to this ticket.
    """

    RED = QColor(255, 0, 0)

    def _overlay(self):
        frame = make_frame(image_size=(200, 200), logical_size=(200, 200))
        overlay = OverlayWindow(frame)
        overlay.set_selection(QRect(0, 0, 200, 200))
        overlay.show()
        QTest.qWaitForWindowExposed(overlay)
        return overlay

    def test_first_escape_with_ink_discards_it_and_toasts_without_closing(self):
        overlay = self._overlay()
        overlay.add_mark(
            Rectangle(colour=self.RED, stroke_width=4, start=QPointF(0, 0), end=QPointF(10, 10))
        )

        QTest.keyClick(overlay, Qt.Key.Key_Escape)

        assert overlay.marks == ()
        assert overlay._toast.isVisible()
        assert overlay._toast._text_label.text() == "Ink discarded"
        assert overlay.isVisible()

    def test_second_escape_with_nothing_left_closes_without_capturing(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            app_module, "copy_image_to_clipboard", lambda image: calls.append(image)
        )
        overlay = self._overlay()
        overlay.add_mark(
            Rectangle(colour=self.RED, stroke_width=4, start=QPointF(0, 0), end=QPointF(10, 10))
        )

        QTest.keyClick(overlay, Qt.Key.Key_Escape)  # ink
        QTest.keyClick(overlay, Qt.Key.Key_Escape)  # the selection, back to choosing
        QTest.keyClick(overlay, Qt.Key.Key_Escape)  # nothing left, closes

        assert not overlay.isVisible()
        assert calls == []  # never captured

    def test_escape_steps_back_to_choosing_before_it_closes(self):
        # Three stages now, not two: ink, then the selection, then out.
        # The post-selection bar carries no mode control -- the handoff's
        # legend reads "Esc back" -- so Esc is the way back to the mode,
        # and cancelling outright would make a mis-picked mode cost the
        # selection too.
        overlay = self._overlay()

        QTest.keyClick(overlay, Qt.Key.Key_Escape)

        assert overlay.isVisible()
        assert overlay._selection is None

        QTest.keyClick(overlay, Qt.Key.Key_Escape)

        assert not overlay.isVisible()


class TestCloseButton:
    """SNX-80 AC: 'the overlay shows a visible control that closes it
    without capturing; using it discards any ink and takes no screenshot;
    it is reachable no matter which tool is selected and whether or not
    ink has been drawn; it does not appear in the exported image; its
    tooltip names Escape.'
    """

    RED = QColor(255, 0, 0)

    def _overlay(self, with_selection=True):
        frame = make_frame(image_size=(200, 200), logical_size=(200, 200))
        overlay = OverlayWindow(frame)
        if with_selection:
            overlay.set_selection(QRect(0, 0, 200, 200))
        overlay.show()
        QTest.qWaitForWindowExposed(overlay)
        return overlay

    def test_visible_before_any_selection_has_been_made(self):
        # `_bar` and the chips above the selection both only exist once a
        # selection does -- this is the one control a user has to back out
        # with before they've dragged anything at all.
        overlay = self._overlay(with_selection=False)

        assert overlay._close_button.isVisible()
        assert not overlay._bar.isVisible()

    def test_stays_visible_regardless_of_active_tool_and_ink_present(self):
        overlay = self._overlay()
        overlay._bar.select_tool("pen")
        overlay.add_mark(
            Rectangle(colour=self.RED, stroke_width=4, start=QPointF(0, 0), end=QPointF(10, 10))
        )

        assert overlay._close_button.isVisible()

    def test_hidden_while_the_overlay_itself_is_not_shown(self):
        # Mirrors `_bar`'s own "stays hidden and unpainted" guarantee --
        # this window is never actually shown, so nothing in it should be
        # either.
        frame = make_frame(image_size=(200, 200), logical_size=(200, 200))
        overlay = OverlayWindow(frame)

        assert not overlay._close_button.isVisible()

    def test_tooltip_names_escape(self):
        overlay = self._overlay()

        assert "Escape" in overlay._close_button.toolTip()

    def test_click_discards_ink_and_closes_without_capturing(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            app_module, "copy_image_to_clipboard", lambda image: calls.append(image)
        )
        overlay = self._overlay()
        overlay.add_mark(
            Rectangle(colour=self.RED, stroke_width=4, start=QPointF(0, 0), end=QPointF(10, 10))
        )

        QTest.mouseClick(overlay._close_button, Qt.MouseButton.LeftButton)

        assert overlay.marks == ()
        assert not overlay.isVisible()
        assert calls == []  # never captured

    def test_click_before_any_selection_still_closes(self):
        overlay = self._overlay(with_selection=False)

        QTest.mouseClick(overlay._close_button, Qt.MouseButton.LeftButton)

        assert not overlay.isVisible()

    def test_excluded_from_the_exported_image(self):
        size = (200, 200)
        frame = make_frame(image_size=size, logical_size=size)
        overlay = OverlayWindow(frame)
        # The selection spans the whole window, including the close
        # button's own fixed corner position -- a leak would show up there
        # as a pixel-colour mismatch against the frame's own base colour.
        overlay.set_selection(QRect(0, 0, *size))
        overlay.show()
        QTest.qWaitForWindowExposed(overlay)

        rendered = overlay.rendered_image()

        button_center = overlay._close_button.geometry().center()
        assert pixel(rendered, button_center) == QColor(10, 20, 30)


class TestKeyboardShortcutSuppression:
    """SNX-47 AC: 'none of these keys fire while a text label is being
    edited or a slider has focus, and the key reaches the focused widget
    instead.' SNX-79 carves Escape and undo/redo out of that suppression --
    see TestEscapeAndUndoRedoBypassSuppression below for those; this class
    now only covers the shortcuts that must keep yielding to a focused
    slider or label.
    """

    RED = QColor(255, 0, 0)

    def _overlay(self):
        frame = make_frame(image_size=(200, 200), logical_size=(200, 200))
        overlay = OverlayWindow(frame)
        overlay.set_selection(QRect(0, 0, 200, 200))
        return overlay

    def test_tool_letter_does_not_fire_while_a_slider_has_focus(self):
        overlay = self._overlay()
        overlay._tray._slider.setFocus()

        QTest.keyClick(overlay, Qt.Key.Key_P)

        assert overlay._bar.active_tool is None

    def test_tool_letter_does_not_fire_while_a_text_label_is_focused(self):
        # A bare QLineEdit stands in for the real text tool's own label
        # editor (`overlay._text_edit`, SNX-52) -- this class only cares
        # that *any* QLineEdit having focus suppresses shortcuts, not that
        # it's specifically the one the text tool builds; see
        # TestDrawingTools.test_text_label_focus_also_suppresses_shortcuts
        # for that narrower case.
        overlay = self._overlay()
        label = QLineEdit(overlay)
        label.setFocus()

        QTest.keyClick(overlay, Qt.Key.Key_P)

        assert overlay._bar.active_tool is None

    def test_enter_does_not_copy_or_close_while_a_slider_has_focus(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            app_module, "copy_image_to_clipboard", lambda image: calls.append(image)
        )
        overlay = self._overlay()
        overlay.show()
        QTest.qWaitForWindowExposed(overlay)
        overlay._tray._slider.setFocus()

        QTest.keyClick(overlay, Qt.Key.Key_Return)

        assert calls == []
        assert overlay.isVisible()

    def test_slider_still_handles_its_own_arrow_key_once_focused(self):
        # The other half of the AC: "a slider being nudged with the arrow
        # keys must keep them." Delivered straight to the slider, the way
        # a real key press would once it actually holds focus -- proving
        # OverlayWindow's own suppression above never has to get involved
        # for the slider's own keys to keep working.
        overlay = self._overlay()
        slider = overlay._tray._slider
        slider.setFocus()
        original = slider.value()

        QTest.keyClick(slider, Qt.Key.Key_Right)

        assert slider.value() == original + slider.singleStep()

    def test_text_label_still_receives_typed_letters_once_focused(self):
        overlay = self._overlay()
        label = QLineEdit(overlay)
        label.setFocus()

        QTest.keyClick(label, "P")

        assert label.text() == "P"


class TestEscapeAndUndoRedoBypassSuppression:
    """SNX-79: Escape stops working once the stroke slider or a label has
    focus, because _shortcuts_suppressed() (SNX-47, see
    TestKeyboardShortcutSuppression above) dropped every shortcut it names
    including Escape. Touching the stroke slider is part of ordinary use, so
    after the first stroke-width change the user had no key left to close
    the overlay. Escape is the way out of a modal, full-screen window and
    must keep working regardless of which child holds focus; undo/redo are
    named in the same acceptance criteria for the same reason.
    """

    RED = QColor(255, 0, 0)

    def _overlay(self):
        frame = make_frame(image_size=(200, 200), logical_size=(200, 200))
        overlay = OverlayWindow(frame)
        overlay.set_selection(QRect(0, 0, 200, 200))
        overlay.show()
        QTest.qWaitForWindowExposed(overlay)
        return overlay

    def test_escape_reaches_the_overlay_while_the_stroke_slider_has_focus(self):
        # The point of this test is that a focused slider does not swallow
        # Escape, not which stage Escape is at -- so it presses through to
        # the close rather than asserting on the first press.
        overlay = self._overlay()
        overlay._tray._slider.setFocus()

        QTest.keyClick(overlay, Qt.Key.Key_Escape)
        QTest.keyClick(overlay, Qt.Key.Key_Escape)

        assert not overlay.isVisible()

    def test_escape_abandons_a_focused_label_then_a_further_escape_closes(self):
        overlay = self._overlay()
        overlay._bar.select_tool("text")
        QTest.mousePress(overlay, Qt.MouseButton.LeftButton, pos=QPoint(30, 30))
        QApplication.processEvents()
        assert overlay._text_edit.isVisible()
        QTest.keyClicks(overlay._text_edit, "hello")

        QTest.keyClick(overlay, Qt.Key.Key_Escape)  # first stage: abandons the label

        assert overlay._text_edit.isHidden()
        assert overlay.marks == ()  # abandoned, not committed as a Text mark
        assert overlay.isVisible()

        QTest.keyClick(overlay, Qt.Key.Key_Escape)  # the selection
        QTest.keyClick(overlay, Qt.Key.Key_Escape)  # nothing left, closes

        assert not overlay.isVisible()

    def test_escape_gets_the_user_out_whether_a_slider_or_a_label_has_focus(self):
        # The acceptance criterion in its own words: focus a slider, then a
        # label, in turn, and Escape must get the user out of the overlay
        # both times.
        overlay = self._overlay()
        overlay._tray._slider.setFocus()

        # Pressed until it is out: the claim is that a focused child never
        # swallows Escape, not how many stages Escape has.
        for _ in range(3):
            QTest.keyClick(overlay, Qt.Key.Key_Escape)

        assert not overlay.isVisible()

        overlay = self._overlay()
        label = QLineEdit(overlay)
        label.setFocus()

        for _ in range(3):
            QTest.keyClick(overlay, Qt.Key.Key_Escape)

        assert not overlay.isVisible()

    def test_undo_still_fires_while_a_slider_has_focus(self):
        overlay = self._overlay()
        overlay.add_mark(
            Rectangle(colour=self.RED, stroke_width=4, start=QPointF(0, 0), end=QPointF(10, 10))
        )
        overlay._tray._slider.setFocus()

        QTest.keyClick(overlay, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)

        assert overlay.marks == ()

    def test_redo_still_fires_while_a_slider_has_focus(self):
        overlay = self._overlay()
        overlay.add_mark(
            Rectangle(colour=self.RED, stroke_width=4, start=QPointF(0, 0), end=QPointF(10, 10))
        )
        overlay.undo()
        overlay._tray._slider.setFocus()

        QTest.keyClick(
            overlay,
            Qt.Key.Key_Z,
            Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier,
        )

        assert len(overlay.marks) == 1

    def test_tool_letters_stay_suppressed_while_a_slider_has_focus_alongside_escape(self):
        # Guards against a fix that stops suppressing everything rather than
        # carving out just Escape and undo/redo -- the AC is explicit that
        # tool letters must stay suppressed.
        #
        # Asserts the tool does not *change*, rather than that none is
        # armed: the toolbar now opens with the pen already armed, so
        # "nothing is active" stopped being a usable proxy for "the key was
        # swallowed" -- pressing P would leave `active_tool == "pen"`
        # whether it was suppressed or not. Highlighter is a letter the
        # default is not.
        overlay = self._overlay()
        before = overlay._bar.active_tool
        overlay._tray._slider.setFocus()

        QTest.keyClick(overlay, Qt.Key.Key_H)

        assert overlay._bar.active_tool == before
        assert overlay._bar.active_tool != "highlighter"

    def test_arrow_key_still_reaches_the_slider_after_the_escape_fix(self):
        overlay = self._overlay()
        slider = overlay._tray._slider
        slider.setFocus()
        original = slider.value()

        QTest.keyClick(slider, Qt.Key.Key_Right)

        assert slider.value() == original + slider.singleStep()


class TestOverlayWindowOnDismissed:
    """SNX-58: `on_dismissed` is how a Wayland multi-monitor group's
    `_MonitorVeil` companions get told to close once the real, interactive
    `OverlayWindow` does -- wired through `closeEvent`, deliberately not
    `hideEvent`, since `_start_delayed_capture` (SNX-50) also plain-hides
    this same window mid-countdown and re-shows it moments later, which
    must not tear the veils down.
    """

    def test_close_calls_on_dismissed(self):
        calls = []
        overlay = OverlayWindow(make_frame(), on_dismissed=lambda: calls.append(1))

        overlay.close()

        assert calls == [1]

    def test_hide_alone_does_not_call_on_dismissed(self):
        calls = []
        overlay = OverlayWindow(make_frame(), on_dismissed=lambda: calls.append(1))

        overlay.hide()

        assert calls == []

    def test_on_dismissed_defaults_to_none_and_close_does_not_raise(self):
        overlay = OverlayWindow(make_frame())

        overlay.close()  # must not raise for lack of a callback


class TestOverlayWindowShowOnScreen:
    """SNX-58 AC: 'the overlay covers the whole screen on Wayland without
    relying on setting its own window position' and 'the overlay is above
    other windows and takes keyboard focus when it opens.'
    """

    def test_none_screen_falls_back_to_a_plain_show(self):
        overlay = OverlayWindow(make_frame())

        overlay.show_on_screen(None)

        assert overlay.isVisible()
        assert not overlay.isFullScreen()

    def test_a_real_screen_requests_fullscreen_instead_of_a_plain_show(self):
        overlay = OverlayWindow(make_frame())
        screen = QApplication.primaryScreen()
        assert screen is not None  # the offscreen platform still reports one

        overlay.show_on_screen(screen)

        assert overlay.isVisible()
        assert overlay.isFullScreen()


class TestMonitorVeil:
    """SNX-58: the non-interactive companion `open_overlay` shows on every
    monitor besides the one the real `OverlayWindow` covers, on Wayland
    with more than one monitor.
    """

    def test_paints_its_own_monitor_frame_dimmed(self):
        image = QImage(100, 50, QImage.Format.Format_RGB32)
        image.fill(BASE_COLOR)
        monitor_frame = Frame(
            image=image, logical_origin=QPointF(200, 0), logical_size=QSizeF(100, 50)
        )

        veil = overlay_module._MonitorVeil(monitor_frame)

        assert veil.size() == QSize(100, 50)
        sampled = pixel(veil.grab().toImage(), 10, 10)
        expected = _blend(QColor(10, 20, 30), overlay_module.Overlay.VEIL_COLOR)
        assert sampled.red() == pytest.approx(expected.red(), abs=2)
        assert sampled.green() == pytest.approx(expected.green(), abs=2)
        assert sampled.blue() == pytest.approx(expected.blue(), abs=2)


class TestOpenOverlay:
    """SNX-58: `open_overlay` is where the session type app.py already
    detected (never assumed, per CLAUDE.md) turns into either a single
    `OverlayWindow` (X11, or a single-monitor Wayland session) or a
    Wayland multi-monitor group -- see its own docstring for the split.
    """

    def test_x11_shows_one_overlay_window_spanning_every_monitor(self, monkeypatch):
        # A `_MonitorVeil` constructed here would mean the X11 path started
        # building a group it has no business building -- X11's single
        # OverlayWindow already covers every monitor on its own, unchanged
        # from before this ticket.
        monkeypatch.setattr(overlay_module, "_MonitorVeil", Mock(side_effect=AssertionError))
        frame = make_frame(image_size=(400, 200), logical_size=(400, 200))
        geometries = [QRectF(0, 0, 200, 200), QRectF(200, 0, 200, 200)]

        result = open_overlay(frame, geometries, wayland=False)

        assert isinstance(result, OverlayWindow)
        assert result.isVisible()
        assert not result.isFullScreen()
        assert result._frame is frame
        assert result._monitor_geometries == geometries

    def test_wayland_single_monitor_uses_the_frame_uncropped(self):
        frame = make_frame(image_size=(200, 200), logical_size=(200, 200))
        geometries = [QRectF(0, 0, 200, 200)]

        result = open_overlay(frame, geometries, wayland=True)

        assert result._frame is frame
        assert result._monitor_geometries == geometries
        assert result._on_dismissed is None

    def test_wayland_multi_monitor_crops_the_primary_window_to_its_own_monitor(self):
        frame = make_frame(image_size=(400, 200), logical_size=(400, 200))
        geometries = [QRectF(0, 0, 200, 200), QRectF(200, 0, 200, 200)]

        result = open_overlay(frame, geometries, wayland=True)

        assert result._frame.logical_origin == geometries[0].topLeft()
        assert result._frame.logical_size == geometries[0].size()
        assert result._monitor_geometries == [geometries[0]]
        assert result._on_dismissed is not None

    def test_wayland_multi_monitor_covers_the_remaining_monitors_with_veils(self, monkeypatch):
        created = []

        class FakeVeil:
            def __init__(self, monitor_frame):
                self.monitor_frame = monitor_frame
                self.closed = False
                created.append(self)

            def show_on_screen(self, screen):
                pass

            def close(self):
                self.closed = True

        monkeypatch.setattr(overlay_module, "_MonitorVeil", FakeVeil)
        frame = make_frame(image_size=(600, 200), logical_size=(600, 200))
        geometries = [
            QRectF(0, 0, 200, 200),
            QRectF(200, 0, 200, 200),
            QRectF(400, 0, 200, 200),
        ]

        open_overlay(frame, geometries, wayland=True)

        # One veil per monitor but the primary, each cropped to its own.
        assert [veil.monitor_frame.logical_origin for veil in created] == [
            geometries[1].topLeft(),
            geometries[2].topLeft(),
        ]
        assert all(not veil.closed for veil in created)

    def test_closing_the_primary_overlay_closes_its_veil_companions(self, monkeypatch):
        created = []

        class FakeVeil:
            def __init__(self, monitor_frame):
                self.closed = False
                created.append(self)

            def show_on_screen(self, screen):
                pass

            def close(self):
                self.closed = True

        monkeypatch.setattr(overlay_module, "_MonitorVeil", FakeVeil)
        frame = make_frame(image_size=(400, 200), logical_size=(400, 200))
        geometries = [QRectF(0, 0, 200, 200), QRectF(200, 0, 200, 200)]

        result = open_overlay(frame, geometries, wayland=True)
        result.close()

        assert created and all(veil.closed for veil in created)


# ---------------------------------------------------------------------------
# Chrome on a staggered multi-monitor desktop
# ---------------------------------------------------------------------------
# The floating bar, both popovers and the settings trays used to be clamped
# against `OverlayWindow.size()` -- the whole virtual desktop on X11, where a
# single window spans every monitor. A union of monitors is not a safe place
# to put chrome: unless every monitor is the same height and mounted at the
# same offset, the union contains gaps that no monitor displays, and chrome
# clamped into one of those gaps is invisible while still being, technically,
# inside the window.
#
# The geometry below is a real three-monitor desktop that shows it: a 1440px
# centre monitor flanked by two 1080px monitors mounted ~200px lower. The
# union is 6400x1440; the flanking monitors stop at y=1281 and y=1268.

STAGGERED_LEFT = QRectF(0, 201, 1920, 1080)      # bottom edge y=1281
STAGGERED_CENTRE = QRectF(1920, 0, 2560, 1440)   # full height, the primary
STAGGERED_RIGHT = QRectF(4480, 188, 1920, 1080)  # bottom edge y=1268
STAGGERED = [STAGGERED_CENTRE, STAGGERED_LEFT, STAGGERED_RIGHT]
STAGGERED_UNION = QRectF(0, 0, 6400, 1440)


class _FakeBrowserProvider(UnsupportedGeometryProvider):
    """A geometry provider that answers about a browser, so these tests
    never depend on one actually being open.

    Subclasses the unsupported provider rather than the ABC: everything
    except the one question under test should keep answering "no", which is
    what the real thing does on a platform that cannot enumerate windows.
    """

    def __init__(self, viewport=None, title="Example — Brave"):
        self._viewport = viewport
        self._title = title

    def browser_viewport(self):
        return None if self._viewport is None else (self._title, self._viewport)


class _FakeScroller:
    """Stands in for a real browser window: records what was asked of it,
    and can refuse the foreground the way Windows lets a window do.
    """

    def __init__(self, focusable=True):
        self.focusable = focusable
        self.scrolls = []
        self.restored = 0
        self.focused = 0

    def take_focus(self):
        self.focused += 1
        return self.focusable

    def scroll(self, notches):
        self.scrolls.append(notches)

    def restore(self):
        self.restored += 1


class TestFullPageCapture:
    """The one capture that is not a single shot of a frozen frame.

    Driven through fakes: no browser, no focus stealing, no real mouse.
    The scroll loop itself is tested in test_scroll.py; what matters here
    is the session around it -- getting out of the way, putting things
    back, and delivering the result.
    """

    @pytest.fixture(autouse=True)
    def _clean_slate(self):
        _close_stray_toplevel_windows()

    VIEWPORT = QRectF(0, 0, 400, 300)

    def _overlay(self, scroller, viewport=None, monkeypatch=None):
        provider = _FakeBrowserProvider(viewport or self.VIEWPORT)
        provider.browser_scroller = lambda: scroller
        frame = make_frame(image_size=(800, 600), logical_size=(800, 600))
        overlay = OverlayWindow(frame, monitor_geometries=[QRectF(0, 0, 800, 600)],
                                geometry_provider=provider)
        overlay.setGeometry(0, 0, 800, 600)
        return overlay

    def _take_whole_page(self, overlay):
        """The real route: choose the mode, flip the switch to Full page,
        then press the bar's primary action -- which is what actually takes
        the shot, exactly as a user does it.
        """
        overlay._dispatch_capture_mode(tokens.BROWSER_MODE)
        overlay._scope_switch.set_scope("full")
        overlay._on_bar_copy()

    def test_a_browser_that_will_not_focus_is_reported_not_scrolled(self):
        # SendInput reaches the focused window and nothing else, so
        # scrolling without the foreground would send input somewhere else
        # entirely -- into whatever the user is actually looking at.
        scroller = _FakeScroller(focusable=False)
        overlay = self._overlay(scroller)

        self._take_whole_page(overlay)

        assert scroller.scrolls == []
        assert scroller.restored == 1, "focus must be handed back anyway"

    def test_focus_and_cursor_are_restored_even_when_it_fails(self):
        scroller = _FakeScroller(focusable=False)
        overlay = self._overlay(scroller)

        self._take_whole_page(overlay)

        assert scroller.restored == 1

    def test_no_browser_at_all_says_so_and_scrolls_nothing(self):
        provider = _FakeBrowserProvider(None)
        provider.browser_scroller = lambda: None
        frame = make_frame(image_size=(800, 600), logical_size=(800, 600))
        overlay = OverlayWindow(frame, monitor_geometries=[QRectF(0, 0, 800, 600)],
                                geometry_provider=provider)

        overlay._dispatch_capture_mode(tokens.BROWSER_MODE)

        assert overlay._selection is None

    def test_the_overlay_is_hidden_while_the_page_scrolls(self):
        # It is covering the page, and the whole point of the rest of this
        # window is to stop the desktop changing under a capture -- here
        # the desktop changing is the capture.
        scroller = _FakeScroller()
        overlay = self._overlay(scroller)
        overlay.show()
        QTest.qWaitForWindowExposed(overlay)
        seen = []

        original = overlay._run_full_page_capture

        def watching(*args, **kwargs):
            seen.append(overlay.isVisible())
            raise ScrollCaptureError("stop here")

        overlay._run_full_page_capture = watching
        self._take_whole_page(overlay)

        assert seen == [False], "the overlay was still up while scrolling"

    def test_it_comes_back_after_a_failure(self):
        # A page that could not be joined is worth being told about, and
        # the user may well want to frame a region by hand instead -- so
        # the snip stays open rather than closing with nothing.
        scroller = _FakeScroller()
        overlay = self._overlay(scroller)
        overlay.show()
        QTest.qWaitForWindowExposed(overlay)
        overlay._run_full_page_capture = lambda *a, **k: (_ for _ in ()).throw(
            ScrollCaptureError("the page was still growing")
        )

        overlay._dispatch_capture_mode(tokens.BROWSER_MODE)

        assert overlay.isVisible()
        assert not overlay.isHidden()

    def test_a_captured_page_is_reported_and_copied(self, monkeypatch):
        copied = []
        monkeypatch.setattr(app_module, "copy_image_to_clipboard", copied.append)
        scroller = _FakeScroller()
        overlay = self._overlay(scroller)
        reported = []
        overlay._on_captured = lambda image, path: reported.append((image, path))
        page = QImage(400, 900, QImage.Format.Format_RGB32)
        page.fill(0x202020)
        overlay._run_full_page_capture = lambda *a, **k: page

        overlay._chooser.set_after("edit")
        self._take_whole_page(overlay)

        assert len(copied) == 1
        assert copied[0].height() == 900
        assert reported and reported[0][1] is None

    def test_the_save_destination_writes_a_file(self, monkeypatch, tmp_path):
        monkeypatch.setattr(app_module.Path, "home", lambda: tmp_path)
        scroller = _FakeScroller()
        overlay = self._overlay(scroller)
        reported = []
        overlay._on_captured = lambda image, path: reported.append(path)
        page = QImage(400, 900, QImage.Format.Format_RGB32)
        page.fill(0x202020)
        overlay._run_full_page_capture = lambda *a, **k: page

        overlay._chooser.set_after("save")
        self._take_whole_page(overlay)

        assert reported and reported[0] is not None
        assert reported[0].suffix == ".png"
        assert reported[0].exists()

    def test_the_mode_is_greyed_without_a_browser(self):
        provider = _FakeBrowserProvider(None)
        frame = make_frame(image_size=(800, 600), logical_size=(800, 600))
        overlay = OverlayWindow(frame, monitor_geometries=[QRectF(0, 0, 800, 600)],
                                geometry_provider=provider)

        assert overlay._chooser._unavailable_reason(tokens.BROWSER_MODE) is not None

    def test_it_is_offered_when_a_browser_is_there(self):
        overlay = self._overlay(_FakeScroller())

        assert overlay._chooser._unavailable_reason(tokens.BROWSER_MODE) is None

    def test_visible_is_the_default_and_crops_rather_than_scrolls(self):
        # The switch starts on Visible, so the ordinary case takes what is
        # on screen and never touches the browser at all.
        scroller = _FakeScroller()
        overlay = self._overlay(scroller)

        overlay._dispatch_capture_mode(tokens.BROWSER_MODE)

        assert overlay._scope_switch.scope == "visible"
        assert not overlay._capturing_whole_page()
        assert scroller.focused == 0, "Visible must not take focus"

    def test_the_switch_is_shown_with_the_outlined_page(self):
        scroller = _FakeScroller()
        overlay = self._overlay(scroller)
        overlay.show()
        QTest.qWaitForWindowExposed(overlay)

        overlay._dispatch_capture_mode(tokens.BROWSER_MODE)

        assert overlay._scope_switch.isVisible()

    def test_framing_a_region_by_hand_takes_the_switch_away(self):
        # Drag anything and the bar is about that rectangle again, whatever
        # the switch last said.
        scroller = _FakeScroller()
        overlay = self._overlay(scroller)
        overlay.show()
        QTest.qWaitForWindowExposed(overlay)
        overlay._dispatch_capture_mode(tokens.BROWSER_MODE)
        overlay._scope_switch.set_scope("full")

        overlay.set_selection(QRect(10, 10, 120, 90))

        assert not overlay._browser_selection
        assert not overlay._capturing_whole_page()
        assert not overlay._scope_switch.isVisible()

    def test_the_switch_starts_fresh_for_each_page(self):
        # Seeding it back to Visible must not read as the user choosing.
        scroller = _FakeScroller()
        overlay = self._overlay(scroller)
        heard = []
        overlay._scope_switch.scopeChanged.connect(heard.append)

        overlay._dispatch_capture_mode(tokens.BROWSER_MODE)

        assert overlay._scope_switch.scope == "visible"
        assert heard == []

    def test_the_drawing_tools_go_away_for_a_whole_page(self):
        # There is nothing to draw on yet, and anything drawn would be
        # thrown away: the capture is assembled from frames grabbed after
        # this window steps aside, so marks on the frozen frame never reach
        # it. Offering the tools would be offering work that disappears.
        scroller = _FakeScroller()
        overlay = self._overlay(scroller)
        overlay.show()
        QTest.qWaitForWindowExposed(overlay)
        overlay._dispatch_capture_mode(tokens.BROWSER_MODE)
        assert overlay._bar._tool_buttons["pen"].isVisible()

        overlay._scope_switch.set_scope("full")

        assert not overlay._bar._tool_buttons["pen"].isVisible()
        assert not overlay._bar._clear_button.isVisible()

    def test_the_tools_come_back_for_the_visible_page(self):
        scroller = _FakeScroller()
        overlay = self._overlay(scroller)
        overlay.show()
        QTest.qWaitForWindowExposed(overlay)
        overlay._dispatch_capture_mode(tokens.BROWSER_MODE)
        overlay._scope_switch.set_scope("full")

        overlay._scope_switch.set_scope("visible")

        assert overlay._bar._tool_buttons["pen"].isVisible()

    def test_framing_a_region_brings_the_tools_back(self):
        scroller = _FakeScroller()
        overlay = self._overlay(scroller)
        overlay.show()
        QTest.qWaitForWindowExposed(overlay)
        overlay._dispatch_capture_mode(tokens.BROWSER_MODE)
        overlay._scope_switch.set_scope("full")

        overlay.set_selection(QRect(10, 10, 120, 90))

        assert overlay._bar._tool_buttons["pen"].isVisible()

    def test_it_says_what_it_is_doing_before_it_disappears(self):
        # This window vanishes for several seconds while the browser
        # scrolls itself, and a desktop that starts moving on its own with
        # no explanation is alarming.
        scroller = _FakeScroller()
        overlay = self._overlay(scroller)
        overlay.show()
        QTest.qWaitForWindowExposed(overlay)
        said = []
        overlay._show_toast = lambda icon, text: said.append(text)
        overlay._run_full_page_capture = lambda *a, **k: (_ for _ in ()).throw(
            ScrollCaptureError("stop")
        )

        self._take_whole_page(overlay)

        assert any("Scrolling" in t for t in said)

    def test_edit_sends_a_whole_page_to_the_review_window(self, monkeypatch):
        # `edit` means "let me annotate this", and for an image taller than
        # the screen that cannot happen on the overlay. The review window
        # is the same tools on a surface that can scroll.
        monkeypatch.setattr(app_module, "copy_image_to_clipboard", lambda i: None)
        scroller = _FakeScroller()
        overlay = self._overlay(scroller)
        page = QImage(400, 2000, QImage.Format.Format_RGB32)
        page.fill(0x202020)
        overlay._run_full_page_capture = lambda *a, **k: page
        overlay._on_captured = lambda image, path: None
        overlay._chooser.set_after("edit")

        self._take_whole_page(overlay)

        assert overlay.outcome == "review"

    def test_instant_still_means_no_window(self, monkeypatch):
        # An explicit request for no window is not overridden just because
        # the image is tall.
        copied = []
        monkeypatch.setattr(app_module, "copy_image_to_clipboard", copied.append)
        scroller = _FakeScroller()
        overlay = self._overlay(scroller)
        page = QImage(400, 2000, QImage.Format.Format_RGB32)
        page.fill(0x202020)
        overlay._run_full_page_capture = lambda *a, **k: page
        overlay._on_captured = lambda image, path: None
        overlay._chooser.set_after("instant")

        self._take_whole_page(overlay)

        assert overlay.outcome == "instant"
        assert len(copied) == 1

    def test_instant_does_not_fire_before_the_scope_is_chosen(self, monkeypatch):
        # `instant` means "finish the moment a selection is made", and for
        # a browser page the selection is not the whole decision -- visible
        # or whole is still to come. Firing on the selection took the
        # visible page and closed before the switch was ever on screen.
        copied = []
        monkeypatch.setattr(app_module, "copy_image_to_clipboard", copied.append)
        overlay = self._overlay(_FakeScroller())
        overlay._chooser.set_after("instant")

        overlay._dispatch_capture_mode(tokens.BROWSER_MODE)

        assert copied == [], "it captured before the switch was offered"
        assert overlay._selection is not None
        assert overlay._browser_selection

    def test_it_is_not_offered_for_recording(self):
        # Recording something that scrolls itself is a different feature,
        # not this one with a video codec on the end.
        overlay = self._overlay(_FakeScroller())
        overlay._chooser.set_kind("record")

        assert overlay._chooser._unavailable_reason(tokens.BROWSER_MODE) is not None


class TestTheTabModeCapturesTheBrowsersPage:
    """`Tab` selects the page area of the frontmost browser -- everything
    below the tab strip and toolbars -- with no rectangle to frame by hand.

    Driven entirely through a fake provider. Nothing here may pass or fail
    by whether a browser happens to be running on the machine.
    """

    @pytest.fixture(autouse=True)
    def _clean_slate(self):
        _close_stray_toplevel_windows()

    # A desktop whose origin is not (0, 0), so a local-for-absolute mix-up
    # is a whole monitor's error rather than an invisible one.
    LEFT = QRectF(-1920, 0, 1920, 1080)
    PRIMARY = QRectF(0, 0, 1920, 1080)
    ORIGIN = (-1920, 0)

    def _overlay(self, viewport=None, size=(3840, 1080), origin=None):
        origin = self.ORIGIN if origin is None else origin
        frame = make_frame(image_size=size, logical_size=size, logical_origin=origin)
        overlay = OverlayWindow(
            frame,
            monitor_geometries=[self.PRIMARY, self.LEFT],
            geometry_provider=_FakeBrowserProvider(viewport),
        )
        overlay.setGeometry(round(origin[0]), round(origin[1]), *size)
        overlay.show()
        QTest.qWaitForWindowExposed(overlay)
        return overlay

    def test_it_selects_the_viewport(self):
        # Absolute (-1720, 300) is window-local (200, 300) against this
        # frame's origin.
        overlay = self._overlay(QRectF(-1720, 300, 1280, 700))

        overlay._dispatch_capture_mode(tokens.BROWSER_MODE)

        assert overlay._selection == QRect(200, 300, 1280, 700)

    def test_the_row_is_offered_when_a_browser_is_found(self):
        assert self._overlay(QRectF(-1720, 300, 1280, 700))._chooser._browser_available

    def test_the_row_is_greyed_when_there_is_none(self):
        assert not self._overlay(None)._chooser._browser_available

    def test_no_browser_selects_nothing(self):
        overlay = self._overlay(None)

        overlay._dispatch_capture_mode(tokens.BROWSER_MODE)

        assert overlay._selection is None

    def test_the_toolbar_lands_on_the_pages_own_monitor(self):
        # No drag means no anchor, so `_chrome_bounds` falls back to
        # largest overlap -- which must still be the left monitor.
        overlay = self._overlay(QRectF(-1720, 300, 1280, 600))

        overlay._dispatch_capture_mode(tokens.BROWSER_MODE)

        assert overlay._selection_anchor is None
        bar = QRectF(overlay._bar.geometry()).translated(QPointF(*self.ORIGIN))
        assert self.LEFT.contains(bar), f"bar at {bar}"

    def test_a_viewport_reaching_past_the_desktop_is_clipped(self):
        # A maximised browser's frame extends past the monitor by the width
        # of its invisible resize border, and the frame is the only source
        # of pixels there is.
        overlay = self._overlay(QRectF(-2000, 300, 1280, 700))

        overlay._dispatch_capture_mode(tokens.BROWSER_MODE)

        assert overlay._selection == QRect(0, 300, 1200, 700)

    def test_a_viewport_entirely_off_the_desktop_selects_nothing(self):
        overlay = self._overlay(QRectF(9000, 9000, 800, 600))

        overlay._dispatch_capture_mode(tokens.BROWSER_MODE)

        assert overlay._selection is None

    def test_the_provider_is_asked_once_when_the_overlay_opens(self):
        # Enumerating windows walks the whole desktop, and the answer
        # cannot change while a frozen frame is on screen -- so asking
        # again at dispatch time would cost a desktop walk to re-learn what
        # is already known, against a desktop that no longer matches the
        # pixels being captured.
        calls = []

        class Counting(_FakeBrowserProvider):
            def browser_viewport(self):
                calls.append(1)
                return super().browser_viewport()

        frame = make_frame(image_size=(3840, 1080), logical_size=(3840, 1080),
                           logical_origin=self.ORIGIN)
        overlay = OverlayWindow(
            frame,
            monitor_geometries=[self.PRIMARY, self.LEFT],
            geometry_provider=Counting(QRectF(-1720, 300, 1280, 700)),
        )
        overlay.setGeometry(-1920, 0, 3840, 1080)
        overlay.show()
        QTest.qWaitForWindowExposed(overlay)

        overlay._dispatch_capture_mode(tokens.BROWSER_MODE)
        overlay._dispatch_capture_mode(tokens.BROWSER_MODE)

        assert calls == [1]

    def test_a_provider_that_knows_nothing_about_browsers_still_works(self):
        # The ABC defaults `browser_viewport` to None so an older provider
        # -- and every platform that cannot enumerate windows -- keeps
        # working rather than raising.
        assert UnsupportedGeometryProvider().browser_viewport() is None


class TestReuseLastRegionPreselectsIt:
    """The `reuse last region` preference: Region mode opens on the
    rectangle the last snip came from instead of an empty overlay.

    A preference, not a fifth mode. Picking a mode from a dropdown every
    time costs the same interaction the drag did, so a mode would have
    spent exactly what it was meant to save.

    Coordinates are the sharp edge here, per CLAUDE.md -- the rectangle is
    stored absolute and used window-local, and these frames deliberately
    have a non-zero origin so a missing translate cannot pass.
    """

    @pytest.fixture(autouse=True)
    def _clean_slate(self):
        _close_stray_toplevel_windows()

    # Two 1920x1080 monitors with the second mounted to the *left* of the
    # primary, so absolute x runs from -1920. A local-for-absolute mix-up
    # is a whole monitor's error.
    LEFT = QRectF(-1920, 0, 1920, 1080)
    PRIMARY = QRectF(0, 0, 1920, 1080)
    MONITORS = [PRIMARY, LEFT]
    ORIGIN = (-1920, 0)

    def _overlay(self, monitors=None, origin=None, size=(3840, 1080)) -> OverlayWindow:
        origin = self.ORIGIN if origin is None else origin
        frame = make_frame(image_size=size, logical_size=size, logical_origin=origin)
        overlay = OverlayWindow(
            frame, monitor_geometries=list(self.MONITORS if monitors is None else monitors)
        )
        overlay.setGeometry(round(origin[0]), round(origin[1]), *size)
        overlay.show()
        QTest.qWaitForWindowExposed(overlay)
        return overlay

    # -- remembering -----------------------------------------------------

    def test_a_finished_snip_is_what_gets_remembered(self):
        overlay = self._overlay()
        # Window-local (200, 300) is absolute (-1720, 300), on the left monitor.
        overlay.set_selection(QRect(200, 300, 640, 480))

        overlay.copy()

        assert setup_desktop.load_last_region() == (-1720, 300, 640, 480)

    def test_an_abandoned_selection_is_not_remembered(self):
        # Why this is recorded at `_report_capture` and not at
        # `_commit_selection`: a rectangle dragged, reconsidered and
        # cancelled is not what anyone means by "the last region".
        overlay = self._overlay()
        overlay.set_selection(QRect(200, 300, 640, 480))

        overlay.close()

        assert setup_desktop.load_last_region() is None

    def test_a_lasso_does_not_overwrite_the_remembered_rectangle(self):
        # A freeform selection's bounding box is not what was captured --
        # everything outside the traced path came out transparent -- so
        # offering that box back as a plain rectangle would recapture an
        # area the user never selected.
        overlay = self._overlay()
        setup_desktop.save_last_region((-1720, 300, 640, 480))
        path = QPainterPath()
        path.addEllipse(QRectF(900, 200, 300, 300))
        overlay.set_selection(QRect(900, 200, 300, 300), path=path)

        overlay.copy()

        assert setup_desktop.load_last_region() == (-1720, 300, 640, 480)

    # -- the preference --------------------------------------------------

    def test_the_row_toggle_is_seeded_from_storage(self):
        # The control has to show the state it is actually in, or the user
        # turns on a preference that was already on.
        setup_desktop.save_reuse_last_region(True)

        assert self._overlay()._chooser.reuse_last_region is True

    def test_clicking_the_row_toggle_persists_it(self):
        # The gap this closes: the toggle was on the row, emitting its
        # signal, with nothing on the other end -- so it looked like a
        # working control and changed nothing at all.
        overlay = self._overlay()

        QTest.mouseClick(
            overlay._chooser.panel.reuse_toggle, Qt.MouseButton.LeftButton
        )

        assert setup_desktop.load_reuse_last_region() is True

    def test_clicking_it_off_again_persists_that_too(self):
        setup_desktop.save_reuse_last_region(True)
        overlay = self._overlay()

        QTest.mouseClick(
            overlay._chooser.panel.reuse_toggle, Qt.MouseButton.LeftButton
        )

        assert setup_desktop.load_reuse_last_region() is False

    def test_turning_it_on_does_not_pre_select_underneath_the_pointer(self):
        # Pre-selecting stands the chooser down, so acting immediately
        # would take the row -- and the toggle just clicked -- off screen.
        setup_desktop.save_last_region((-1720, 300, 640, 480))
        overlay = self._overlay()

        QTest.mouseClick(
            overlay._chooser.panel.reuse_toggle, Qt.MouseButton.LeftButton
        )

        assert overlay._selection is None

    def test_it_takes_effect_on_the_next_overlay(self):
        setup_desktop.save_last_region((-1720, 300, 640, 480))
        first = self._overlay()
        QTest.mouseClick(
            first._chooser.panel.reuse_toggle, Qt.MouseButton.LeftButton
        )

        assert self._overlay()._selection == QRect(200, 300, 640, 480)

    def test_the_preference_is_off_unless_asked_for(self):
        # Pre-selecting an area the user did not ask for this time changes
        # what the first frame of a snip means, so it is never a default.
        setup_desktop.save_last_region((-1720, 300, 640, 480))

        assert self._overlay()._selection is None

    def test_with_it_on_the_overlay_opens_on_the_last_rectangle(self):
        setup_desktop.save_last_region((-1720, 300, 640, 480))
        setup_desktop.save_reuse_last_region(True)

        overlay = self._overlay()

        # Absolute (-1720, 300) is window-local (200, 300) here.
        assert overlay._selection == QRect(200, 300, 640, 480)

    def test_it_needs_no_trip_through_the_mode_menu(self):
        # The whole point of the redesign: nothing is picked, nothing is
        # clicked, and the mode is still the plain Region it always was.
        setup_desktop.save_last_region((-1720, 300, 640, 480))
        setup_desktop.save_reuse_last_region(True)

        overlay = self._overlay()

        assert overlay._chooser.mode == "Region"
        # Reuse is a preference on the row, never a mode of its own -- the
        # point of the redesign. `Tab` is a mode because it captures
        # something different, not because it changes how Region behaves.
        assert "Last region" not in [m[0] for m in tokens.CAPTURE_MODES]

    def test_the_toolbar_is_up_on_the_rectangles_own_monitor(self):
        setup_desktop.save_last_region((-1720, 300, 640, 480))
        setup_desktop.save_reuse_last_region(True)

        overlay = self._overlay()

        assert overlay._bar.isVisible()
        bar = QRectF(overlay._bar.geometry()).translated(QPointF(*self.ORIGIN))
        assert self.LEFT.contains(bar), f"bar at {bar}"

    def test_nothing_is_captured_merely_by_opening(self):
        # A pre-selection is not a commit. `instant` finishes the snip the
        # moment a selection is *committed*, and an overlay that opened
        # must not have finished anything.
        setup_desktop.save_last_region((-1720, 300, 640, 480))
        setup_desktop.save_reuse_last_region(True)
        reported = []
        frame = make_frame(
            image_size=(3840, 1080), logical_size=(3840, 1080), logical_origin=self.ORIGIN
        )
        overlay = OverlayWindow(
            frame,
            monitor_geometries=list(self.MONITORS),
            on_captured=lambda image, path: reported.append(path),
        )
        overlay._chooser.set_after("instant")
        overlay.setGeometry(-1920, 0, 3840, 1080)
        overlay.show()
        QTest.qWaitForWindowExposed(overlay)

        assert reported == []
        assert overlay.isVisible()

    def test_dragging_a_new_box_replaces_the_recalled_one(self):
        # "Still draggable" is the half that keeps the preference from
        # becoming a mode you have to remember to turn off: a press
        # outside the selection starts a fresh one, exactly as it does for
        # any other selection.
        setup_desktop.save_last_region((-1720, 300, 640, 480))
        setup_desktop.save_reuse_last_region(True)
        overlay = self._overlay()

        start_at, end_at = QPoint(2400, 100), QPoint(2900, 500)
        QTest.mousePress(
            overlay, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, start_at
        )
        QTest.mouseMove(overlay, end_at)
        QTest.mouseRelease(
            overlay, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, end_at
        )

        assert overlay._selection == QRect(2400, 100, 500, 400)

    def test_a_press_inside_a_recalled_region_reframes_rather_than_draws(self):
        # The trap this closes: the recalled rectangle can be the size of a
        # whole monitor, leaving nowhere outside it to press -- and with a
        # tool armed, every press inside it drew. There was then no way to
        # frame anything else short of Esc.
        setup_desktop.save_last_region((-1900, 20, 1900, 1040))
        setup_desktop.save_reuse_last_region(True)
        overlay = self._overlay()
        assert overlay._selection == QRect(20, 20, 1900, 1040)

        start_at, end_at = QPoint(400, 300), QPoint(900, 700)
        QTest.mousePress(
            overlay, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, start_at
        )
        QTest.mouseMove(overlay, end_at)
        QTest.mouseRelease(
            overlay, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, end_at
        )

        assert overlay._selection == QRect(400, 300, 500, 400)
        assert not overlay.marks, "the press drew instead of reframing"

    def test_no_tool_is_armed_over_a_recalled_region(self):
        setup_desktop.save_last_region((-1720, 300, 640, 480))
        setup_desktop.save_reuse_last_region(True)

        overlay = self._overlay()

        assert overlay._bar.active_tool is None

    def test_the_pen_still_arms_for_a_region_the_user_draws(self):
        # The latch must not be spent by the recalled selection, or
        # reframing would cost the default tool.
        setup_desktop.save_last_region((-1720, 300, 640, 480))
        setup_desktop.save_reuse_last_region(True)
        overlay = self._overlay()

        start_at, end_at = QPoint(2400, 100), QPoint(2900, 500)
        QTest.mousePress(
            overlay, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, start_at
        )
        QTest.mouseMove(overlay, end_at)
        QTest.mouseRelease(
            overlay, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, end_at
        )

        assert overlay._bar.active_tool == "pen"

    def test_picking_a_tool_adopts_the_recalled_region(self):
        # Reaching for a tool means the framing is accepted, so presses
        # inside it draw from then on -- which is the point of picking one.
        setup_desktop.save_last_region((-1900, 20, 1900, 1040))
        setup_desktop.save_reuse_last_region(True)
        overlay = self._overlay()

        overlay._bar.select_tool("pen")
        QTest.mousePress(
            overlay, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
            QPoint(400, 300),
        )
        QTest.mouseMove(overlay, QPoint(600, 500))
        QTest.mouseRelease(
            overlay, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
            QPoint(600, 500),
        )

        assert overlay._selection == QRect(20, 20, 1900, 1040), "reframed instead of drawing"
        assert len(overlay.marks) == 1

    def test_resizing_a_recalled_region_adopts_it_too(self):
        setup_desktop.save_last_region((-1720, 300, 640, 480))
        setup_desktop.save_reuse_last_region(True)
        overlay = self._overlay()

        # Press the bottom-right corner handle of the recalled rectangle.
        corner = QPoint(200 + 640, 300 + 480)
        QTest.mousePress(
            overlay, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, corner
        )
        QTest.mouseMove(overlay, QPoint(corner.x() + 60, corner.y() + 40))
        QTest.mouseRelease(
            overlay, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
            QPoint(corner.x() + 60, corner.y() + 40),
        )

        assert overlay._recalled_selection is False
        assert overlay._selection != QRect(200, 300, 640, 480)

    def test_the_record_side_is_left_alone(self):
        # Committing is what arms a recording, and arming one as a side
        # effect of opening the overlay is not something anyone asked for.
        setup_desktop.save_last_region((-1720, 300, 640, 480))
        setup_desktop.save_reuse_last_region(True)
        setup_desktop.save_kind("record")

        overlay = self._overlay()

        assert overlay._selection is None
        assert overlay._armed_for_recording is False

    # -- recalling onto a desktop that has changed -----------------------

    def test_nothing_remembered_yet_leaves_an_ordinary_empty_overlay(self):
        setup_desktop.save_reuse_last_region(True)

        assert self._overlay()._selection is None

    def test_a_rectangle_reaching_past_a_shrunken_desktop_is_clipped(self):
        # Remembered on the two-monitor desk, recalled with the left
        # monitor unplugged. The frame is the only source of pixels there
        # is, so the part that no longer exists is cut off rather than
        # cropped from nothing.
        setup_desktop.save_last_region((-200, 300, 640, 480))
        setup_desktop.save_reuse_last_region(True)

        overlay = self._overlay(
            monitors=[QRectF(self.PRIMARY)], origin=(0, 0), size=(1920, 1080)
        )

        assert overlay._selection == QRect(0, 300, 440, 480)

    def test_a_rectangle_on_a_monitor_that_is_gone_is_discarded(self):
        setup_desktop.save_last_region((-1720, 300, 640, 480))
        setup_desktop.save_reuse_last_region(True)

        overlay = self._overlay(
            monitors=[QRectF(self.PRIMARY)], origin=(0, 0), size=(1920, 1080)
        )

        assert overlay._selection is None

    def test_a_rectangle_surviving_only_in_a_gap_is_discarded(self):
        # The frame's span is the *union* of the monitors, and a staggered
        # desk leaves gaps inside that union which no display shows. A
        # rectangle surviving only there would crop black pixels.
        setup_desktop.save_last_region((300, 20, 200, 100))
        setup_desktop.save_reuse_last_region(True)

        overlay = self._overlay(
            monitors=list(STAGGERED), origin=(0, 0), size=(6400, 1440)
        )

        assert overlay._selection is None


class TestTheDestinationIsRemembered:
    """Last used wins. The handoff made the chooser and the split action's
    caret one-snip overrides that never wrote back, and that read as the
    control being ignored -- pick Copy, take the snip, and the next one is
    back on Open.
    """

    @pytest.fixture(autouse=True)
    def _clean_slate(self):
        _close_stray_toplevel_windows()

    def _overlay(self) -> OverlayWindow:
        frame = make_frame(image_size=(800, 600), logical_size=(800, 600))
        overlay = OverlayWindow(frame)
        overlay.setGeometry(0, 0, 800, 600)
        overlay.show()
        QTest.qWaitForWindowExposed(overlay)
        return overlay

    def test_choosing_a_destination_stores_it(self):
        overlay = self._overlay()

        overlay._chooser.set_after("save")

        assert setup_desktop.load_after_capture() == "save"

    def test_the_next_overlay_opens_on_it(self):
        first = self._overlay()
        first._chooser.set_after("save")

        assert self._overlay().outcome == "save"

    def test_the_carets_choice_is_stored_too(self, monkeypatch):
        # The control actually reached for in the report.
        monkeypatch.setattr(app_module, "copy_image_to_clipboard", lambda image: None)
        overlay = self._overlay()
        overlay._chooser.set_after("review")
        overlay.set_selection(QRect(100, 100, 300, 250))

        overlay._on_destination_chosen("Copy")

        assert setup_desktop.load_after_capture() == "edit"

    def test_seeding_from_settings_is_not_mistaken_for_a_choice(self):
        # Adopting what is already stored must not write it straight back,
        # or every overlay opened would look like a fresh decision.
        written = []
        original = setup_desktop.save_after_capture
        try:
            setup_desktop.save_after_capture = lambda *a, **k: written.append(a)
            self._overlay()
        finally:
            setup_desktop.save_after_capture = original

        assert written == []

    def test_the_record_side_keeps_its_own_answer(self):
        # `after` on the record side is a different vocabulary with its own
        # stored key -- writing one into the other would seed the stills
        # chooser from a value its own menu cannot show.
        overlay = self._overlay()
        overlay._chooser.set_kind("record")

        overlay._chooser.set_after("open")

        assert setup_desktop.load_recording_after() == "open"
        assert setup_desktop.load_after_capture() == tokens.AFTER_DEFAULT


class TestTheChooserRowFollowsThePointer:
    """The row is placed against one monitor, and which one used to be
    settled when the overlay opened -- so pressing the shortcut while
    working on one screen and then crossing to another to frame something
    left every control back where you started.

    `_cursor_pos` is set directly rather than through `QTest.mouseMove`:
    synthesising a *hover* onto a window that is not the OS-active one does
    not reliably deliver on Windows (conftest documents the same gap for
    the cursor-shape tests), and this behaviour is not about event
    plumbing.
    """

    @pytest.fixture(autouse=True)
    def _clean_slate(self):
        _close_stray_toplevel_windows()

    def _overlay(self) -> OverlayWindow:
        frame = make_frame(image_size=(6400, 1440), logical_size=(6400, 1440))
        overlay = OverlayWindow(frame, monitor_geometries=list(STAGGERED))
        overlay.setGeometry(0, 0, 6400, 1440)
        overlay.show()
        QTest.qWaitForWindowExposed(overlay)
        return overlay

    @staticmethod
    def _row(overlay: OverlayWindow) -> QRectF:
        return QRectF(overlay._chooser.panel.geometry())

    def _point_at(self, overlay: OverlayWindow, monitor: QRectF) -> None:
        overlay._cursor_pos = QPointF(monitor.center())
        overlay._follow_pointer_to_its_monitor()

    @pytest.mark.parametrize(
        "monitor", [STAGGERED_LEFT, STAGGERED_CENTRE, STAGGERED_RIGHT]
    )
    def test_the_row_moves_to_the_monitor_under_the_pointer(self, monitor):
        overlay = self._overlay()

        self._point_at(overlay, monitor)

        assert monitor.contains(self._row(overlay)), (
            f"row at {self._row(overlay)} is not on {monitor}"
        )

    def test_it_follows_across_several_monitors_in_turn(self):
        overlay = self._overlay()

        for monitor in (STAGGERED_RIGHT, STAGGERED_LEFT, STAGGERED_CENTRE):
            self._point_at(overlay, monitor)
            assert monitor.contains(self._row(overlay))

    def test_a_move_within_one_monitor_does_not_relayout(self):
        # This runs from every mouse-move event, so it has to be cheap: a
        # row re-laid out on every pixel would repaint across the frozen
        # frame for no visible change.
        overlay = self._overlay()
        self._point_at(overlay, STAGGERED_CENTRE)
        before = self._row(overlay)
        calls = []
        overlay._sync_chooser_visibility = lambda: calls.append(1)

        overlay._cursor_pos = QPointF(STAGGERED_CENTRE.center()) + QPointF(40, 30)
        overlay._follow_pointer_to_its_monitor()

        assert calls == []
        assert self._row(overlay) == before

    def test_the_row_stands_still_once_something_is_selected(self):
        # From here the floating bar is the chrome, and it is anchored to
        # where the drag started on purpose -- chrome that chased the
        # pointer mid-drag would be worse than chrome that stayed put.
        overlay = self._overlay()
        self._point_at(overlay, STAGGERED_LEFT)
        overlay.set_selection(QRect(300, 600, 400, 300))
        calls = []
        overlay._sync_chooser_visibility = lambda: calls.append(1)

        self._point_at(overlay, STAGGERED_RIGHT)

        assert calls == []

    def test_nothing_happens_before_the_pointer_has_ever_moved(self):
        # `_cursor_pos` is None until the first real move, and the opening
        # placement is `_active_screen_rect`'s own job.
        overlay = self._overlay()
        calls = []
        overlay._sync_chooser_visibility = lambda: calls.append(1)

        overlay._follow_pointer_to_its_monitor()

        assert calls == []

    def test_the_active_screen_prefers_the_tracked_pointer(self):
        # Not `QCursor.pos()`: this file refuses global cursor state so a
        # test never has to drive a system-wide pointer.
        overlay = self._overlay()
        overlay._cursor_pos = QPointF(STAGGERED_RIGHT.center())

        assert overlay._active_screen_rect() == STAGGERED_RIGHT


class TestChromeStaysOnTheSelectionsMonitor:
    """Every piece of floating chrome is clamped to the monitor the
    selection is on, never to the union of every monitor.
    """

    @pytest.fixture(autouse=True)
    def _clean_slate(self):
        _close_stray_toplevel_windows()

    def _overlay(self, selection: QRect) -> OverlayWindow:
        frame = make_frame(image_size=(6400, 1440), logical_size=(6400, 1440))
        overlay = OverlayWindow(frame, monitor_geometries=list(STAGGERED))
        overlay.setGeometry(0, 0, 6400, 1440)
        overlay.set_selection(selection)
        overlay.show()
        QTest.qWaitForWindowExposed(overlay)
        return overlay

    @staticmethod
    def _on_a_monitor(rect: QRect) -> bool:
        return any(QRectF(m).contains(QRectF(rect)) for m in STAGGERED)

    def test_bar_for_a_selection_low_on_a_short_monitor_stays_on_it(self):
        # The regression. The old clamp allowed a top of
        # union.height() - _TOP_MAX_FROM_BOTTOM == 1322, which is 41px
        # below this monitor's own bottom edge -- so the bar was drawn
        # into the gap beneath it and never appeared on any screen.
        overlay = self._overlay(QRect(300, 1000, 600, 250))

        bar = overlay._bar.geometry()

        assert self._on_a_monitor(bar), f"bar at {bar} is on no monitor"
        assert QRectF(STAGGERED_LEFT).contains(QRectF(bar))
        assert bar.bottom() <= STAGGERED_LEFT.bottom()

    def test_bar_low_on_the_other_short_monitor_stays_on_it_too(self):
        overlay = self._overlay(QRect(4700, 1000, 600, 250))

        bar = overlay._bar.geometry()

        assert self._on_a_monitor(bar), f"bar at {bar} is on no monitor"
        assert bar.bottom() <= STAGGERED_RIGHT.bottom()

    def test_bar_near_an_inner_edge_does_not_straddle_the_bezel(self):
        # Selection hard against the left monitor's inner edge. Clamped to
        # the union, the bar centred on it and spilled across x=1920 onto
        # the centre monitor -- split down the middle by the bezel.
        overlay = self._overlay(QRect(1500, 600, 380, 300))

        bar = overlay._bar.geometry()

        assert QRectF(STAGGERED_LEFT).contains(QRectF(bar))
        assert bar.right() <= STAGGERED_LEFT.right()

    def test_bar_on_the_full_height_centre_monitor_is_unaffected(self):
        # The single-monitor-shaped case that always worked, and must keep
        # working: nothing here should have moved.
        overlay = self._overlay(QRect(2600, 500, 700, 400))

        bar = overlay._bar.geometry()

        assert QRectF(STAGGERED_CENTRE).contains(QRectF(bar))
        expected_top = QRectF(QRect(2600, 500, 700, 400)).bottom() + tokens.Metric.BAR_OFFSET_Y
        assert bar.top() == expected_top

    def test_chrome_bounds_picks_the_monitor_holding_most_of_the_selection(self):
        # Straddles the bezel, but three quarters of it is on the centre
        # monitor, so that is where the chrome belongs.
        overlay = self._overlay(QRect(1820, 400, 400, 300))

        assert overlay._chrome_bounds() == STAGGERED_CENTRE

    def test_chrome_bounds_is_the_union_only_when_nothing_overlaps(self):
        # Entirely inside the gap above the left monitor -- no monitor
        # overlaps it at all, and `_monitor_at`'s own last resort is the
        # frame's full span.
        overlay = self._overlay(QRect(300, 20, 200, 100))

        assert overlay._chrome_bounds() == STAGGERED_UNION

    def test_settings_tray_stays_on_the_selections_monitor(self):
        overlay = self._overlay(QRect(300, 950, 600, 250))
        overlay._bar.select_tool(sorted(tokens.DRAW_TOOLS)[0])

        tray = overlay._tray.geometry()

        assert self._on_a_monitor(tray), f"tray at {tray} is on no monitor"

    def test_capture_popover_stays_on_the_selections_monitor(self):
        overlay = self._overlay(QRect(300, 1000, 600, 250))

        QTest.mouseClick(overlay._bar._chip, Qt.MouseButton.LeftButton)
        popover = overlay._popover.geometry()

        assert self._on_a_monitor(popover), f"popover at {popover} is on no monitor"

    def _drag(self, overlay, start: QPoint, end: QPoint) -> None:
        QTest.mousePress(
            overlay, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, start
        )
        for step in (0.3, 0.6, 1.0):
            QTest.mouseMove(
                overlay,
                QPoint(
                    round(start.x() + (end.x() - start.x()) * step),
                    round(start.y() + (end.y() - start.y()) * step),
                ),
            )
        QTest.mouseRelease(
            overlay, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, end
        )

    def _empty_overlay(self) -> OverlayWindow:
        frame = make_frame(image_size=(6400, 1440), logical_size=(6400, 1440))
        overlay = OverlayWindow(frame, monitor_geometries=list(STAGGERED))
        overlay.setGeometry(0, 0, 6400, 1440)
        overlay.show()
        QTest.qWaitForWindowExposed(overlay)
        return overlay

    def test_toolbar_stays_on_the_monitor_the_drag_started_on(self):
        # A drag begun on the left monitor and carried a little past the
        # bezel. Slightly more of the finished rectangle lands on the centre
        # monitor, so "largest overlap" handed the toolbar to the centre --
        # moving the controls off the screen being worked on for a reason
        # invisible to the user. The monitor the drag *started* on is what
        # "the monitor I ran the selection on" means.
        overlay = self._empty_overlay()

        self._drag(overlay, QPoint(1184, 243), QPoint(2779, 1017))

        assert overlay._selection == QRect(1184, 243, 1595, 774)
        bar = overlay._bar.geometry()
        assert QRectF(STAGGERED_LEFT).contains(QRectF(bar)), f"bar at {bar}"

    def test_a_drag_started_on_the_right_monitor_keeps_its_toolbar_there(self):
        overlay = self._empty_overlay()

        self._drag(overlay, QPoint(5200, 400), QPoint(4200, 900))

        bar = overlay._bar.geometry()
        assert QRectF(STAGGERED_RIGHT).contains(QRectF(bar)), f"bar at {bar}"

    def test_a_picked_selection_has_no_anchor_and_uses_largest_overlap(self):
        # Window / Full screen produce a rect outright rather than by
        # dragging, so there is no "monitor I started on" to honour.
        overlay = self._overlay(QRect(1820, 400, 400, 300))

        assert overlay._selection_anchor is None
        assert overlay._chrome_bounds() == STAGGERED_CENTRE

    def test_clearing_the_selection_forgets_where_it_started(self):
        overlay = self._empty_overlay()
        self._drag(overlay, QPoint(300, 600), QPoint(900, 900))
        assert overlay._selection_anchor is not None

        overlay.set_selection(None)

        assert overlay._selection_anchor is None

    def test_cancel_button_is_visible_with_no_selection_yet(self):
        # The regression the user actually hit: a fresh overlay, nothing
        # selected. SNX-80's close button went to the top-right corner of
        # the *window* -- (6350, 16) here, which is 172px above the
        # rightmost monitor's top edge and therefore on no screen at all.
        # The overlay came up dimmed with no visible control anywhere.
        frame = make_frame(image_size=(6400, 1440), logical_size=(6400, 1440))
        overlay = OverlayWindow(frame, monitor_geometries=list(STAGGERED))
        overlay.setGeometry(0, 0, 6400, 1440)
        overlay.show()
        QTest.qWaitForWindowExposed(overlay)

        button = overlay._close_button.geometry()

        assert overlay._close_button.isVisible()
        assert self._on_a_monitor(button), f"cancel button at {button} is on no monitor"

    def test_cancel_button_follows_the_selection_to_its_monitor(self):
        overlay = self._overlay(QRect(300, 700, 600, 250))

        button = overlay._close_button.geometry()

        assert QRectF(STAGGERED_LEFT).contains(QRectF(button))
        assert button.top() >= STAGGERED_LEFT.top()

    def test_toast_confirms_on_the_selections_monitor(self):
        # "Bottom centre of the window" is the bottom centre of the whole
        # virtual desktop once one window spans every monitor -- so saving
        # a snip taken on the left monitor used to confirm it in the middle
        # of the centre one, ~2900px away from what the user was looking at.
        overlay = self._overlay(QRect(300, 700, 600, 250))

        overlay._show_toast("save", "Saved to ~/Pictures/snipux")
        toast = overlay._toast.geometry()

        assert self._on_a_monitor(toast), f"toast at {toast} is on no monitor"
        assert QRectF(STAGGERED_LEFT).contains(QRectF(toast))

    def test_popover_flip_threshold_is_measured_from_its_own_monitor(self):
        # The spec's "bar top > 300px" is a distance from the top of the
        # screen the user is looking at. Read as an absolute window
        # coordinate it also counted the 201px this monitor is mounted
        # down the virtual desktop, flipping the popover upward far too
        # early -- off the top of the monitor.
        overlay = self._overlay(QRect(300, 250, 600, 120))

        QTest.mouseClick(overlay._bar._chip, Qt.MouseButton.LeftButton)
        popover = overlay._popover.geometry()

        assert self._on_a_monitor(popover), f"popover at {popover} is on no monitor"
        assert popover.top() >= STAGGERED_LEFT.top()


class TestOverlayHoldsTheWholeVirtualDesktop:
    """The overlay must keep the exact size of the frame it is showing.

    `paintEvent` draws the frozen frame with
    `drawImage(QRectF(self.rect()), ...)`, so the window's size *is* the
    scale the whole capture is drawn at. GNOME/Mutter treats a plain
    managed window's geometry as a suggestion and shrinks one this large to
    a single monitor's work area -- a 6400x1440 request came back as
    2560x1337. Every monitor then got crushed into that, and every
    coordinate the user dragged in was off by the same factor.

    There is no window manager under the offscreen platform these tests run
    on, so the resize itself cannot be reproduced here; what is asserted is
    the mechanism that prevents it -- fixed (min == max) size hints, which
    is what a WM honours.
    """

    def test_size_hints_pin_the_window_to_the_frames_size(self):
        frame = make_frame(image_size=(6400, 1440), logical_size=(6400, 1440))

        overlay = OverlayWindow(frame)

        assert overlay.minimumSize() == QSize(6400, 1440)
        assert overlay.maximumSize() == QSize(6400, 1440)

    def test_a_resize_cannot_shrink_the_window(self):
        # What Mutter attempts: shrink it to one monitor's work area.
        frame = make_frame(image_size=(6400, 1440), logical_size=(6400, 1440))
        overlay = OverlayWindow(frame)

        overlay.resize(2560, 1337)

        assert overlay.size() == QSize(6400, 1440)

    def test_the_frozen_frame_is_drawn_at_one_to_one(self):
        # The consequence that matters: with the window pinned, the frame is
        # drawn unscaled, so a selection means what it says.
        frame = make_frame(image_size=(6400, 1440), logical_size=(6400, 1440))
        overlay = OverlayWindow(frame)
        overlay.resize(2560, 1337)

        assert overlay.width() == frame.image.width()
        assert overlay.height() == frame.image.height()


class TestWaylandPicksTheInteractiveMonitor:
    """SNX-58 left the one interactive Wayland window on
    `monitor_geometries[0]` -- whatever `QGuiApplication.screens()` listed
    first, which Qt does not promise is the primary screen.
    """

    def test_prefers_the_primary_screen_over_the_first_entry(self):
        primary = QRectF(QApplication.primaryScreen().geometry())
        elsewhere = QRectF(primary.right() + 100, 0, 640, 480)
        # Primary deliberately second, the case the old `[0]` got wrong.
        assert overlay_module._interactive_geometry([elsewhere, primary]) == primary

    def test_falls_back_to_the_first_entry_when_no_entry_is_the_primary(self):
        # Synthetic geometries matching no real screen -- every other test
        # in this file, and the offscreen platform generally.
        first = QRectF(0, 0, 200, 200)
        second = QRectF(200, 0, 200, 200)
        assert overlay_module._interactive_geometry([first, second]) == first

    def test_handles_an_empty_geometry_list(self):
        assert overlay_module._interactive_geometry([]) == QRectF()

    def test_veils_cover_every_monitor_except_the_interactive_one(self, monkeypatch):
        # The `[1:]` slice this replaced was only correct while the
        # interactive monitor was always the first entry: once it can be
        # any entry, slicing leaves the chosen monitor veiled *and* one
        # other monitor uncovered.
        veiled: list[QRectF] = []

        class FakeVeil:
            def __init__(self, monitor_frame):
                veiled.append(
                    QRectF(monitor_frame.logical_origin, monitor_frame.logical_size)
                )

            def show_on_screen(self, screen):
                pass

            def close(self):
                pass

        monkeypatch.setattr(overlay_module, "_MonitorVeil", FakeVeil)
        first = QRectF(0, 0, 200, 200)
        chosen = QRectF(200, 0, 200, 200)
        third = QRectF(400, 0, 200, 200)
        monkeypatch.setattr(
            overlay_module, "_interactive_geometry", lambda geometries: chosen
        )
        frame = make_frame(image_size=(600, 200), logical_size=(600, 200))

        result = open_overlay(frame, [first, chosen, third], wayland=True)
        result.close()

        assert veiled == [first, third]


class TestPressOutsideStartsANewSelection:
    """A press on the dimmed area outside the current selection begins a
    fresh region drag.

    It used to be a no-op, which left Esc as the only way out of a
    selection placed slightly wrong -- and Esc cancels the whole snip,
    frozen frame included, so a misplaced drag cost the entire capture.
    """

    @pytest.fixture(autouse=True)
    def _clean_slate(self):
        _close_stray_toplevel_windows()

    def _overlay(self) -> OverlayWindow:
        frame = make_frame(image_size=(800, 600), logical_size=(800, 600))
        overlay = OverlayWindow(frame)
        overlay.setGeometry(0, 0, 800, 600)
        overlay.show()
        QTest.qWaitForWindowExposed(overlay)
        return overlay

    def _drag(self, overlay, start: QPoint, end: QPoint) -> None:
        QTest.mousePress(
            overlay, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, start
        )
        for step in (0.5, 1.0):
            QTest.mouseMove(
                overlay,
                QPoint(
                    round(start.x() + (end.x() - start.x()) * step),
                    round(start.y() + (end.y() - start.y()) * step),
                ),
            )
        QTest.mouseRelease(
            overlay, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, end
        )

    def test_a_second_drag_outside_the_first_replaces_the_selection(self):
        overlay = self._overlay()
        self._drag(overlay, QPoint(80, 80), QPoint(280, 230))
        assert overlay._selection == QRect(80, 80, 200, 150)

        self._drag(overlay, QPoint(450, 350), QPoint(650, 500))

        assert overlay._selection == QRect(450, 350, 200, 150)

    def test_the_new_selection_can_be_dragged_back_over_the_old_one(self):
        overlay = self._overlay()
        self._drag(overlay, QPoint(400, 300), QPoint(600, 450))

        # Starts outside the existing selection, finishes across it.
        self._drag(overlay, QPoint(100, 100), QPoint(500, 380))

        assert overlay._selection == QRect(100, 100, 400, 280)

    def test_a_press_inside_the_selection_still_draws_rather_than_restarting(self):
        overlay = self._overlay()
        self._drag(overlay, QPoint(100, 100), QPoint(500, 400))
        before = QRect(overlay._selection)
        overlay._bar.select_tool(sorted(tokens.DRAW_TOOLS)[0])

        self._drag(overlay, QPoint(200, 200), QPoint(300, 300))

        assert overlay._selection == before, "a stroke must not restart the selection"
        assert overlay._marks, "the stroke should have been committed"

    def test_marks_survive_starting_a_new_selection(self):
        # Marks are in window coordinates, so a new selection must not
        # destroy them -- Ctrl+Z could not bring them back.
        overlay = self._overlay()
        self._drag(overlay, QPoint(100, 100), QPoint(500, 400))
        overlay._bar.select_tool(sorted(tokens.DRAW_TOOLS)[0])
        self._drag(overlay, QPoint(200, 200), QPoint(300, 300))
        marks_before = len(overlay._marks)
        assert marks_before

        self._drag(overlay, QPoint(600, 450), QPoint(700, 550))

        assert len(overlay._marks) == marks_before

    def test_dragging_a_handle_still_resizes_instead_of_restarting(self):
        overlay = self._overlay()
        self._drag(overlay, QPoint(100, 100), QPoint(400, 300))
        handle = overlay._corner_hit_rect(Handle.BOTTOM_RIGHT).center().toPoint()

        self._drag(overlay, handle, QPoint(500, 400))

        assert overlay._selection.topLeft() == QPoint(100, 100), "resize, not restart"
        assert overlay._selection.width() > 300


class TestAPressOnChromeIsNotAPressOnTheOverlay:
    """The bar, the trays, the popovers, the toast and the HUD swallow
    their own presses -- see `overlay._Chrome`.

    The class above is the rule this one is the exception to: a press on
    the dimmed frame starts a fresh region drag, which is right for the
    frame and ruinous for the chrome sitting on it. Missing a tool button
    by a pixel and hitting the bar's own background threw away the
    selection the user had just dragged out.
    """

    @pytest.fixture(autouse=True)
    def _clean_slate(self):
        _close_stray_toplevel_windows()

    def _overlay(self) -> OverlayWindow:
        frame = make_frame(image_size=(800, 600), logical_size=(800, 600))
        overlay = OverlayWindow(frame)
        overlay.setGeometry(0, 0, 800, 600)
        overlay.show()
        QTest.qWaitForWindowExposed(overlay)
        return overlay

    def _selected(self, overlay) -> QRect:
        """A real drag, the way the app's own only path to a selection
        goes -- never `set_selection`, which would prove nothing about the
        press that follows it.
        """
        QTest.mousePress(overlay, Qt.MouseButton.LeftButton, pos=QPoint(150, 150))
        QTest.mouseMove(overlay, QPoint(550, 450))
        QTest.mouseRelease(overlay, Qt.MouseButton.LeftButton, pos=QPoint(550, 450))
        assert overlay._selection == QRect(150, 150, 400, 300)
        return QRect(overlay._selection)

    @staticmethod
    def _background_of(widget) -> QPoint:
        """A point on `widget` that belongs to `widget` itself rather than
        to one of its buttons -- the near miss this is all about.
        """
        pos = QPoint(widget.width() - 4, widget.height() // 2)
        assert widget.childAt(pos) is None, "meant to be its own background"
        return pos

    def _press(self, widget, pos=None) -> None:
        QTest.mousePress(
            widget,
            Qt.MouseButton.LeftButton,
            pos=pos or QPoint(widget.width() // 2, widget.height() // 2),
        )

    def test_a_press_on_the_bars_own_background_keeps_the_selection(self):
        overlay = self._overlay()
        before = self._selected(overlay)

        self._press(overlay._bar, self._background_of(overlay._bar))

        assert overlay._selection == before
        assert overlay._region_drag_anchor is None

    def test_a_press_on_a_divider_inside_the_bar_stops_at_the_bar(self):
        # The dividers, pills and separators carry nothing of their own:
        # their presses reach a container that consumes, which is why
        # `_Chrome` is only on the containers.
        overlay = self._overlay()
        before = self._selected(overlay)
        divider = overlay._bar.findChild(_Divider)

        self._press(divider)

        assert overlay._selection == before

    def test_a_press_on_the_popovers_background_neither_closes_nor_draws(self):
        # The popover opens over the selection, so a leaked press here
        # lands where the armed tool draws -- it has to be armed for this
        # to be asking anything.
        overlay = self._overlay()
        before = self._selected(overlay)
        overlay._bar.select_tool(sorted(tokens.DRAW_TOOLS)[0])
        QTest.mouseClick(overlay._bar._chip, Qt.MouseButton.LeftButton)
        assert overlay._popover.isVisible()

        self._press(overlay._popover, self._background_of(overlay._popover))

        assert overlay._popover.isVisible(), "only a press outside closes it"
        assert overlay._selection == before
        assert overlay._in_progress_shape is None

    def test_a_press_on_the_toast_keeps_the_selection(self):
        overlay = self._overlay()
        before = self._selected(overlay)
        overlay._show_toast("copy", "Copied to clipboard")

        self._press(overlay._toast)

        assert overlay._selection == before

    def test_a_press_on_the_hud_keeps_the_selection(self):
        # Full-width chrome, so this is the one that costs something: the
        # strip it covers can no longer be dragged through. A toolbar
        # behaves the same way everywhere else.
        overlay = self._overlay()
        overlay.set_hints_enabled(True)
        before = self._selected(overlay)
        assert overlay._hud.isVisible()

        self._press(overlay._hud)

        assert overlay._selection == before

    def test_the_frame_around_the_chrome_still_starts_a_new_selection(self):
        # The fix must not spread: everything that is not chrome still
        # behaves the way the class above documents.
        overlay = self._overlay()
        self._selected(overlay)

        QTest.mousePress(overlay, Qt.MouseButton.LeftButton, pos=QPoint(650, 500))
        QTest.mouseMove(overlay, QPoint(720, 560))
        QTest.mouseRelease(overlay, Qt.MouseButton.LeftButton, pos=QPoint(720, 560))

        assert overlay._selection == QRect(650, 500, 70, 60)


class TestOverlayIsRevealedNotAnimatedOpen:
    """Mutter stages a newly mapped window by scaling it up into place. Over
    a frozen desktop that reads as a page expanding across the very area
    being captured, so the compositor is left to play that animation on
    something invisible.
    """

    def _overlay(self):
        frame = make_frame(image_size=(800, 600), logical_size=(800, 600))
        return OverlayWindow(frame)

    def test_it_maps_fully_transparent(self):
        overlay = self._overlay()

        overlay.show_on_screen(None)

        assert overlay.windowOpacity() == 0.0

    def test_the_reveal_brings_it_to_full_opacity(self):
        overlay = self._overlay()
        overlay.show_on_screen(None)

        overlay._reveal()

        assert overlay.windowOpacity() == 1.0

    def test_closing_goes_transparent_before_unmapping(self):
        # Mutter stages an unmap the same way it stages a map -- scaling the
        # window down and away -- which is the expanding page in reverse.
        # The opacity has to be set before the window is withdrawn, or the
        # compositor shrinks something still visible.
        overlay = self._overlay()
        overlay.show_on_screen(None)
        overlay._reveal()
        assert overlay.windowOpacity() == 1.0

        overlay.close()

        assert overlay.windowOpacity() == 0.0

    def test_closing_is_synchronous(self):
        # Deferring the close a frame to fade would let a second shortcut
        # press inside that gap be refused as "an overlay is already open":
        # close() is what tells AppController the session is over.
        overlay = self._overlay()
        overlay.show_on_screen(None)

        overlay.close()

        assert not overlay.isVisible()

    def test_closing_still_reports_the_dismissal_exactly_once(self):
        frame = make_frame(image_size=(800, 600), logical_size=(800, 600))
        calls = []
        overlay = OverlayWindow(frame, on_dismissed=lambda: calls.append(True))
        overlay.show_on_screen(None)

        overlay.close()

        assert calls == [True]

    def test_a_reveal_after_it_closed_does_not_revive_it(self):
        # Esc, or a forwarded second request, can close the window inside
        # the delay; bringing it back by opacity alone would be worse than
        # the animation ever was.
        overlay = self._overlay()
        overlay.show_on_screen(None)
        overlay.close()

        overlay._reveal()

        assert not overlay.isVisible()


class TestCaptureChooser:
    """The pre-snip chooser, per docs/design/handoff-chooser.md.

    Two phases and the transitions between them; the widget's own painting
    is the prototype's business, not these tests'.
    """

    def _overlay(self, size=(1200, 800)):
        # Clamped to the screen this process actually has. `_reserved_top`
        # resolves a monitor through `QGuiApplication.screenAt()`, so a
        # synthetic desktop bigger than the real (offscreen) screen puts
        # its own centre off-screen, that lookup misses, and the overlay
        # silently reserves nothing -- which is indistinguishable here from
        # the bug these tests exist to catch. The offscreen screen shrinks
        # as the scale factor rises, so leaving this unclamped pins the
        # class to a scale factor of 1.0.
        available = QGuiApplication.primaryScreen().geometry()
        size = (min(size[0], available.width()), min(size[1], available.height()))
        frame = make_frame(image_size=size, logical_size=size)
        # With a provider that can answer, Window is a mode this session
        # actually has. Without one the overlay refuses it and puts every
        # surface back to Region -- correct, and not what this class is
        # asking about.
        overlay = OverlayWindow(
            frame, geometry_provider=_FakeWindowProvider(QRectF(0, 0, 100, 100))
        )
        overlay.setGeometry(0, 0, *size)
        overlay.show()
        QTest.qWaitForWindowExposed(overlay)
        return overlay

    def test_it_starts_in_choosing_with_the_panel_up(self):
        overlay = self._overlay()

        assert overlay._chooser.phase == "choosing"
        assert overlay._chooser.panel.isVisibleTo(overlay)

    def test_picking_a_mode_arms_it_and_collapses_to_the_tab(self):
        # Region, Window and Freeform all need the screen back -- one to
        # drag on, one to hover over, one to trace across.
        overlay = self._overlay()

        overlay._chooser.set_mode("Window")

        assert overlay._chooser.phase == "armed"
        assert overlay._chooser.tab.isVisibleTo(overlay)
        assert not overlay._chooser.panel.isVisibleTo(overlay)

    def test_the_armed_mode_reaches_the_overlay(self):
        overlay = self._overlay()

        overlay._chooser.set_mode("Freeform")

        assert overlay._capture_mode == "Freeform"

    def test_full_screen_fires_instead_of_arming(self):
        # It has nothing left to aim at, so choosing it *is* the capture --
        # tokens.IMMEDIATE_MODES carries that rather than a name check.
        overlay = self._overlay()
        fired = []
        overlay._chooser.fireImmediately.connect(fired.append)

        overlay._chooser.set_mode("Full screen")

        assert fired == ["Full screen"]
        assert overlay._chooser.phase == "choosing", "it never arms"

    def test_the_tab_reopens_the_panel_with_selections_intact(self):
        overlay = self._overlay()
        overlay._chooser.set_after("clip")
        overlay._chooser.set_mode("Window")

        overlay._chooser.reopen()

        assert overlay._chooser.phase == "choosing"
        assert overlay._chooser.panel.isVisibleTo(overlay)
        assert overlay._chooser.mode == "Window"
        assert overlay._chooser.after == "clip"

    @pytest.mark.parametrize("key,mode", list(tokens.MODE_KEYS.items()))
    def test_each_shortcut_selects_its_mode(self, key, mode):
        overlay = self._overlay()
        # `Tab` is greyed, and its key correspondingly inert, without a
        # browser -- seeded so this test stays about the key map rather
        # than that rule, which test_chooser.py covers on its own.
        overlay._chooser.set_browser_available(True)
        fired = []
        overlay._chooser.fireImmediately.connect(fired.append)

        overlay._chooser.handle_key(ord(key), key)

        assert overlay._chooser.mode == mode

    def test_space_reopens_from_armed(self):
        overlay = self._overlay()
        overlay._chooser.set_mode("Window")

        overlay._chooser.handle_key(Qt.Key.Key_Space, " ")

        assert overlay._chooser.phase == "choosing"

    def test_escape_with_no_menu_open_cancels_the_snip(self):
        overlay = self._overlay()
        cancelled = []
        overlay._chooser.cancelled.connect(lambda: cancelled.append(True))

        overlay._chooser.handle_key(Qt.Key.Key_Escape, "")

        assert cancelled == [True]

    def test_a_key_that_is_not_the_choosers_is_declined(self):
        # It gets first refusal, not the whole keyboard.
        overlay = self._overlay()

        assert overlay._chooser.handle_key(Qt.Key.Key_Z, "z") is False

    def test_it_stands_down_once_there_is_a_selection(self):
        # Chooser up means no selection; bar up means one exists. They never
        # coexist, so they may safely share a widget stack.
        overlay = self._overlay()

        overlay.set_selection(QRect(100, 100, 300, 200))

        assert not overlay._chooser.panel.isVisibleTo(overlay)
        assert not overlay._chooser.tab.isVisibleTo(overlay)
        assert overlay._bar.isVisibleTo(overlay)

    def test_the_bars_chip_is_seeded_from_it(self):
        # One piece of state, two surfaces -- do not duplicate it.
        overlay = self._overlay()

        overlay._chooser.set_mode("Freeform")

        assert overlay._bar._chip._text_label.text() == "Freeform"

    def test_seeding_back_from_the_bar_does_not_rearm(self):
        # Arming on the way back would re-emit into the handler that sent
        # it, which is an infinite loop rather than a design.
        overlay = self._overlay()
        overlay._chooser.reopen()

        overlay._chooser.set_mode("Window", arm=False)

        assert overlay._chooser.mode == "Window"
        assert overlay._chooser.phase == "choosing"

    def test_the_panel_hangs_from_the_active_monitors_top_edge(self):
        # Never the virtual desktop: on a staggered multi-monitor setup its
        # centre is a gap between screens.
        overlay = self._overlay()

        panel = overlay._chooser.panel.geometry()
        screen = overlay._active_screen_rect()
        assert panel.top() == round(screen.y() - overlay.geometry().top())
        assert abs(panel.center().x() - (screen.center().x() - overlay.geometry().left())) <= 2

    def test_the_panel_clears_whatever_the_desktop_reserves_up_there(
        self, monkeypatch
    ):
        # GNOME paints its top bar over an always-on-top window, so a panel
        # hung flush against that edge is behind it -- 32px of a 54px panel
        # on the measured desktop, and all 26px of the armed tab.
        monkeypatch.setattr(
            overlay_module.platform.current, "reserved_top", lambda screen: 32
        )
        overlay = self._overlay()

        screen = overlay._active_screen_rect()
        top = round(screen.y() - overlay.geometry().top())
        assert overlay._chooser.panel.geometry().top() == top + 32

    def test_the_armed_tab_clears_it_too(self, monkeypatch):
        monkeypatch.setattr(
            overlay_module.platform.current, "reserved_top", lambda screen: 32
        )
        overlay = self._overlay()

        overlay._chooser.set_mode("Freeform")

        screen = overlay._active_screen_rect()
        top = round(screen.y() - overlay.geometry().top())
        assert overlay._chooser.tab.geometry().top() == top + 32

    def test_the_close_button_clears_it_as_well(self, monkeypatch):
        # Same corner, same bar: a close button under it cannot be clicked,
        # and it is the only visible way to cancel a snip.
        overlay = self._overlay()
        flush = overlay._close_button.geometry().top()
        monkeypatch.setattr(
            overlay_module.platform.current, "reserved_top", lambda screen: 32
        )
        overlay._reserved_top_cache.clear()

        overlay._reposition_close_button()

        assert overlay._close_button.geometry().top() == flush + 32

    def test_the_delay_defaults_to_none_and_is_selectable(self):
        overlay = self._overlay()
        assert overlay._chooser.delay == tokens.DELAY_DEFAULT

        overlay._chooser.set_delay("5s")

        assert overlay._chooser.delay == "5s"


class TestTheChooserTakesItsOwnClicks:
    """SNX-108: a press on the chooser must never reach the overlay.

    Everything above drives the chooser through `set_mode`/`reopen`, and
    that is exactly the seam this bug lived in: the state machine was right
    and no click could get to it. A widget that leaves a press unaccepted
    hands it to its parent -- the overlay -- which reads a press with no
    selection as the start of a region drag, so clicking `Region` armed a
    region capture and dropped the user into the overlay with the chooser
    gone. These press the widgets themselves.
    """

    def _overlay(self, size=(1200, 800)):
        frame = make_frame(image_size=size, logical_size=size)
        overlay = OverlayWindow(frame)
        overlay.setGeometry(0, 0, *size)
        overlay.show()
        QTest.qWaitForWindowExposed(overlay)
        return overlay

    @staticmethod
    def _centre(widget):
        return QPoint(widget.width() // 2, widget.height() // 2)

    def _press(self, widget):
        QTest.mousePress(widget, Qt.MouseButton.LeftButton, pos=self._centre(widget))

    def _click(self, widget):
        QTest.mouseClick(widget, Qt.MouseButton.LeftButton, pos=self._centre(widget))

    @pytest.mark.parametrize(
        "trigger", ["mode_trigger", "after_trigger", "delay_trigger"]
    )
    def test_a_press_on_a_trigger_starts_no_capture(self, trigger):
        overlay = self._overlay()

        self._press(getattr(overlay._chooser.panel, trigger))

        # A selection at all -- even the empty one a drag opens with -- is
        # what stands the chooser down and puts the floating bar up.
        assert overlay._selection is None
        assert overlay._chooser.panel.isVisibleTo(overlay)

    def test_a_press_on_the_panel_itself_starts_no_capture(self):
        # The gaps between the triggers and the word "then" are the panel's
        # own background, and a press that lands there is still a press on
        # the chooser.
        overlay = self._overlay()

        self._press(overlay._chooser.panel)

        assert overlay._selection is None
        assert overlay._chooser.panel.isVisibleTo(overlay)

    def test_a_press_on_the_armed_tab_starts_no_capture(self):
        overlay = self._overlay()
        overlay._chooser.set_mode("Freeform")

        self._press(overlay._chooser.tab)

        assert overlay._selection is None
        assert overlay._chooser.tab.isVisibleTo(overlay)

    def test_clicking_a_trigger_opens_its_menu(self):
        overlay = self._overlay()

        self._click(overlay._chooser.panel.mode_trigger)

        assert overlay._chooser._menu_kind == "mode"

    def test_clicking_through_to_a_row_arms_that_mode(self):
        # The whole gesture the video showed failing: click the trigger,
        # then click a row in the menu it opened.
        # Freeform rather than Window: a headless session has no window
        # geometry provider, so Window would fall back to Region and prove
        # nothing about the click that got there.
        overlay = self._overlay()

        self._click(overlay._chooser.panel.mode_trigger)
        self._click(overlay._chooser._menu._rows["Freeform"])

        assert overlay._chooser.mode == "Freeform"
        assert overlay._chooser.phase == "armed"
        assert overlay._capture_mode == "Freeform"

    def test_clicking_the_tab_reopens_the_panel(self):
        overlay = self._overlay()
        overlay._chooser.set_mode("Freeform")

        self._click(overlay._chooser.tab)

        assert overlay._chooser.phase == "choosing"

    def test_sliding_off_a_trigger_before_releasing_is_not_a_click(self):
        # Consuming the press makes this widget Qt's implicit mouse
        # grabber, so the release comes back here wherever it happens.
        # Pressing a control and sliding away from it means "no".
        overlay = self._overlay()
        trigger = overlay._chooser.panel.mode_trigger

        self._press(trigger)
        QTest.mouseRelease(trigger, Qt.MouseButton.LeftButton, pos=QPoint(-200, 400))

        assert overlay._chooser._menu is None

    def test_sliding_off_a_menu_row_before_releasing_picks_nothing(self):
        overlay = self._overlay()
        self._click(overlay._chooser.panel.mode_trigger)
        row = overlay._chooser._menu._rows["Freeform"]

        QTest.mousePress(row, Qt.MouseButton.LeftButton, pos=self._centre(row))
        QTest.mouseRelease(row, Qt.MouseButton.LeftButton, pos=QPoint(-200, 400))

        assert overlay._chooser.mode == "Region"
        assert overlay._chooser.phase == "choosing"
        assert overlay._capture_mode == "Region"

    def test_double_clicking_a_trigger_starts_no_capture(self):
        # Qt sends a second press as a `MouseButtonDblClick`, and a widget
        # that ignores that gets the press-propagating default back --
        # which is the same leak by another event type. Clicking twice
        # because nothing seemed to happen is exactly how the bug was hit.
        overlay = self._overlay()
        trigger = overlay._chooser.panel.mode_trigger

        QTest.mouseDClick(
            trigger, Qt.MouseButton.LeftButton, pos=self._centre(trigger)
        )

        assert overlay._selection is None
        assert overlay._chooser.panel.isVisibleTo(overlay)


class TestTheDestinationMenuChangesTheDestination:
    """Picking a destination from the split action's caret has to change
    what actually happens to the snip, not just what the button says.

    `app.py._on_captured` reads `OverlayWindow.outcome` -- the *chooser's*
    destination -- to decide whether the review window opens. So a caret
    that re-faced the button without telling the chooser left the old
    destination in force, and the menu looked broken while behaving
    exactly as written.
    """

    @pytest.fixture(autouse=True)
    def _clean_slate(self):
        _close_stray_toplevel_windows()

    def _overlay(self, monkeypatch, tmp_path, after: str) -> OverlayWindow:
        monkeypatch.setattr(app_module, "copy_image_to_clipboard", lambda image: None)
        monkeypatch.setattr(app_module.Path, "home", lambda: tmp_path)
        frame = make_frame(image_size=(800, 600), logical_size=(800, 600))
        overlay = OverlayWindow(frame)
        overlay.setGeometry(0, 0, 800, 600)
        overlay.show()
        QTest.qWaitForWindowExposed(overlay)
        # The chooser said "open the review window" before the snip, which
        # is what puts `Open` on the split button's face.
        overlay._chooser.set_after(after)
        overlay.set_selection(QRect(100, 100, 300, 250))
        return overlay

    def test_the_face_starts_as_the_chooser_left_it(self, monkeypatch, tmp_path):
        overlay = self._overlay(monkeypatch, tmp_path, "review")

        assert overlay._bar.destination() == "Open"

    def test_choosing_copy_stops_the_review_window_opening(self, monkeypatch, tmp_path):
        # The reported bug: "cant seem to change the option here? it just
        # always open it in the editor." Copy went to the clipboard *and*
        # the review window opened anyway, because `outcome` still said
        # review.
        overlay = self._overlay(monkeypatch, tmp_path, "review")
        seen = []
        overlay._on_captured = lambda image, path: seen.append(overlay.outcome)

        overlay._on_destination_chosen("Copy")

        assert seen == ["edit"], "the snip finished still claiming review"

    def test_choosing_save_stops_the_review_window_opening(self, monkeypatch, tmp_path):
        overlay = self._overlay(monkeypatch, tmp_path, "review")
        seen = []
        overlay._on_captured = lambda image, path: seen.append(overlay.outcome)

        overlay._on_destination_chosen("Save")

        assert seen == ["save"]

    def test_choosing_open_still_asks_for_the_review_window(self, monkeypatch, tmp_path):
        # The other direction, from a chooser that did *not* ask for it.
        overlay = self._overlay(monkeypatch, tmp_path, "instant")
        seen = []
        overlay._on_captured = lambda image, path: seen.append(overlay.outcome)

        overlay._on_destination_chosen("Open")

        assert seen == ["review"]

    def test_the_face_follows_the_choice(self, monkeypatch, tmp_path):
        overlay = self._overlay(monkeypatch, tmp_path, "review")

        overlay._on_destination_chosen("Save")

        assert overlay._bar.destination() == "Save"

    def test_the_caret_opens_the_menu(self, monkeypatch, tmp_path):
        # The other half of "cannot change the option": if the caret never
        # opened a menu there would be nothing to pick from in the first
        # place.
        overlay = self._overlay(monkeypatch, tmp_path, "review")
        action = overlay._bar._action

        QTest.mouseClick(
            action,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
            QPoint(action.width() - 8, action.height() // 2),
        )

        menu = getattr(overlay, "_destination_menu", None)
        assert menu is not None and menu.isVisible()
        menu.close()

    def test_pressing_the_face_is_not_the_caret(self, monkeypatch, tmp_path):
        # The seam has to actually separate the two halves, or every
        # attempt to open the menu fires the destination instead -- which
        # would read as "it just always opens it in the editor" too.
        overlay = self._overlay(monkeypatch, tmp_path, "review")
        action = overlay._bar._action
        fired = []
        overlay._bar.openRequested.connect(lambda: fired.append("open"))

        QTest.mouseClick(
            action,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
            QPoint(8, action.height() // 2),
        )

        assert fired == ["open"]


class TestTheDestinationMenuFitsItsWidth:
    """The menu is a fixed 270px and its notes are prose.

    A note that overruns is not a cosmetic problem: it prints over the tick
    that says which destination is selected, so the row stops answering the
    one question it exists to answer.
    """

    def _budget(self):
        from PyQt6.QtGui import QFontMetricsF

        from snipux.chooser import _font

        metric = tokens.ChooserMetric
        _pad_v, pad_h = metric.MENU_ROW_PAD
        text_x = pad_h + metric.MENU_ROW_ICON + 9
        width = (
            metric.MENU_AFTER_W
            - 2 * metric.MENU_PAD
            - text_x
            - pad_h
            - (metric.MENU_TICK + 8)
        )
        return width, QFontMetricsF(_font(11, 400)), QFontMetricsF(_font(12.5, 500))

    def test_every_note_fits_without_eliding(self):
        # This budget is measured against the font `_font()` actually
        # resolves (matching production's own paintEvent), not a hard-coded
        # number -- but IBM Plex isn't vendored yet (design/fonts/ doesn't
        # exist in this handoff), so the resolved substitute is whatever
        # *this machine's* own GeneralFont fallback happens to be. That is a
        # fact about the box the suite runs on, not the OS family: gating
        # this on sys.platform (an earlier version of this test did) is
        # exactly backwards, since a narrow-enough Linux fallback is not
        # guaranteed either -- only that *some* machines have one. Skip only
        # when the assertion would actually fail *and* the reason is the
        # known one (no real Plex installed), rather than assuming a whole
        # OS one way or the other; production already elides safely when a
        # note doesn't fit (see chooser.py's `_MenuRow.paintEvent`), so this
        # is a no-eliding-needed guarantee that only holds where a narrow
        # enough font is actually available, not a regression.
        width, note_fm, _label_fm = self._budget()

        too_wide = {
            value: note
            for value, note in tokens.CHOOSER_AFTER_NOTE.items()
            if note_fm.horizontalAdvance(note) > width
        }

        resolved_ui = font_families().ui
        if too_wide and resolved_ui != tokens.Font.UI:
            pytest.skip(
                "IBM Plex Sans is not registered on this box (design/fonts/ "
                f"isn't vendored yet), so this budget is measured against "
                f"whatever fallback font ({resolved_ui!r}) this machine's "
                "Qt install substitutes instead -- wide enough here to "
                "overrun the panel, which production already elides safely "
                "for (see chooser.py's `_MenuRow.paintEvent`)."
            )

        assert too_wide == {}, f"notes wider than {width}px: {too_wide}"

    def test_every_label_fits_without_eliding(self):
        width, _note_fm, label_fm = self._budget()

        from snipux.chooser import _AFTER_ROWS

        too_wide = {
            label: label_fm.horizontalAdvance(label)
            for _value, _icon, label, _note in _AFTER_ROWS
            if label_fm.horizontalAdvance(label) > width
        }

        assert too_wide == {}

    def test_the_notes_cover_exactly_the_destinations_offered(self):
        # Two surfaces, two lengths of prose, one list of destinations.
        assert set(tokens.CHOOSER_AFTER_NOTE) == {
            value for value, _label, _description in tokens.AFTER_CAPTURE
        }

    def test_an_overlong_note_is_elided_rather_than_overrunning(self):
        from snipux.chooser import _MenuRow

        row = _MenuRow("review", "eye", "Review", "x" * 400)
        row.resize(tokens.ChooserMetric.MENU_AFTER_W, 40)

        row.grab()  # a full paintEvent; it must not paint past its own edge


class TestTheArmedCursorInvitesTheDrag:
    """handoff-chooser.md, Armed: "The cursor becomes a crosshair."

    Region is the case that matters. Window and Freeform repaint the cursor
    on every mouse move as part of previewing, but Region has nothing to
    preview -- so without this it sits under a plain arrow for exactly as
    long as the user is deciding whether to drag.
    """

    def _overlay(self, size=(1200, 800)):
        frame = make_frame(image_size=size, logical_size=size)
        overlay = OverlayWindow(frame)
        overlay.setGeometry(0, 0, *size)
        overlay.show()
        QTest.qWaitForWindowExposed(overlay)
        return overlay

    def test_choosing_leaves_an_ordinary_arrow(self):
        # The panel is a thing to click, not an area to drag across.
        overlay = self._overlay()

        assert overlay._chooser.phase == "choosing"
        assert overlay.cursor().shape() == Qt.CursorShape.ArrowCursor

    def test_arming_region_turns_the_pointer_into_a_crosshair(self):
        overlay = self._overlay()

        overlay._chooser.set_mode("Region")

        assert overlay._chooser.phase == "armed"
        assert overlay.cursor().shape() == Qt.CursorShape.CrossCursor

    def test_reopening_the_chooser_gives_the_arrow_back(self):
        overlay = self._overlay()
        overlay._chooser.set_mode("Region")

        overlay._chooser.reopen()
        overlay._apply_idle_cursor()

        assert overlay._chooser.phase == "choosing"
        assert overlay.cursor().shape() == Qt.CursorShape.ArrowCursor

    def test_a_selection_takes_the_cursor_back_over(self):
        # Once there is a rectangle, the handle and inside-the-selection
        # rules own the pointer; the armed crosshair must not override them.
        overlay = self._overlay()
        overlay._chooser.set_mode("Region")

        overlay.set_selection(QRect(100, 100, 300, 200))
        overlay._apply_idle_cursor()

        assert overlay.cursor().shape() == Qt.CursorShape.ArrowCursor
