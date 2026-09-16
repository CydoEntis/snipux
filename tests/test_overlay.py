import ctypes
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PyQt6.QtCore import QEvent, QPoint, QPointF, QRect, QRectF, QSize, QSizeF, Qt, QMargins, QMarginsF
from PyQt6.QtGui import (
    QColor,
    QFont,
    QFontMetricsF,
    QGuiApplication,
    QIcon,
    QImage,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QRegion,
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
import snipux.output as output_module
import snipux.overlay as overlay_module
from snipux.sensitive import RecognizedWord
from conftest import skip_on_windows
from snipux import capture as capture_module
from snipux import design
from snipux import setup_desktop
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
    Redact,
    Arrow,
    Blur,
    Callout,
    Ellipse,
    Highlighter,
    Line,
    ObscuringShape,
    Pen,
    Pixelate,
    Rectangle,
    Spotlight,
    StepMarker,
    Text,
)
from snipux.marks import ToolStyle, ToolStyles, session_styles
from snipux.overlay import (
    CaptureModePopover,
    DelayCountdown,
    FamilyMenu,
    FloatingBar,
    GeometryProvider,
    Handle,
    HintHUD,
    Overlay,
    OverlayWindow,
    SelectionMode,
    StylePopover,
    Toast,
    UnsupportedGeometryProvider,
    _CaptureModeRow,
    _CustomColorButton,
    _CycleButton,
    _DelayRow,
    _Divider,
    _HANDLE_CURSORS,
    _MenuSeparator,
    _PillButton,
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


class TestUnsupportedGeometryProvider:
    def test_is_unavailable_and_reports_no_windows(self):
        provider = UnsupportedGeometryProvider()

        assert provider.is_available() is False
        assert provider.window_at(QPointF(10, 10)) is None


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

        confirmed.assert_called_once_with(self.WINDOW_RECT)

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
            QRectF(QPointF(self.MISS_POINT), QPointF(end)).normalized())

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

        confirmed.assert_called_once_with(self.WINDOW_RECT)

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
            QRectF(QPointF(start), QPointF(end)).normalized())


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

        confirmed.assert_called_once_with(QRectF(30, 30, 50, 50))


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
        monkeypatch.setattr("sys.platform", "win32")
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

        confirmed.assert_called_once_with(QRectF(30, 30, 50, 50))


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

        confirmed.assert_called_once_with(QRectF(0, 0, 200, 200))

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
        overlay._styles.update("ellipse", colour="#ff0000", size=6)

        QTest.mousePress(overlay, Qt.MouseButton.LeftButton, pos=QPoint(80, 80))
        QTest.mouseMove(overlay, QPoint(120, 120))
        QTest.mouseRelease(overlay, Qt.MouseButton.LeftButton, pos=QPoint(120, 120))

        result = overlay.rendered_image()

        # (80, 100) in window coordinates -- the ellipse's own leftmost
        # point, vertically centred -- is (30, 50) inside the crop.
        assert pixel(result, 30, 50) == self.RED


class TestExportedStrokeWidth:
    """#72: a mark exports as thick as the overlay painted it.

    Runs at whatever scale the suite does -- it is kept green at
    QT_SCALE_FACTOR=1 and 1.5 -- over a frame the size a real capture at
    that scale would be: the screen's physical pixels, not its logical
    ones. Every width is read straight across the stroke in each image's
    own physical pixels, the overlay's through its grab and the export's
    off the image it produces.
    """

    RED = QColor(255, 0, 0)
    LOGICAL = (400, 300)
    SELECTION = QRect(40, 60, 320, 200)

    def _overlay(self):
        ratio = QGuiApplication.primaryScreen().devicePixelRatio()
        width, height = self.LOGICAL
        frame = make_frame(
            image_size=(round(width * ratio), round(height * ratio)),
            logical_size=self.LOGICAL,
        )
        overlay = OverlayWindow(frame)
        overlay.set_selection(self.SELECTION)
        return overlay, ratio

    @staticmethod
    def _red_width(image, y, x_from, x_to):
        """How many pixels along row `y` are redder than halfway between the
        frame's own colour and the ink: a stroke's width, wherever its
        antialiased edges fall. Arguments are `image`'s own pixels."""
        reds = [image.pixelColor(x, y).red() for x in range(x_from, x_to)]
        half = (max(reds) + min(reds)) / 2
        return sum(1 for red in reds if red > half)

    @pytest.mark.parametrize("stroke_width", [3, 6, 12])
    @pytest.mark.parametrize("kind", [Line, Rectangle])
    def test_a_mark_exports_as_thick_as_the_overlay_painted_it(self, kind, stroke_width):
        overlay, ratio = self._overlay()
        # An upright stroke at logical x=120 either way: the line itself, or
        # the rectangle's left side.
        end = QPointF(120, 220) if kind is Line else QPointF(280, 220)
        overlay.add_mark(
            kind(colour=self.RED, stroke_width=stroke_width, start=QPointF(120, 100), end=end)
        )

        on_screen = overlay.grab().toImage()
        exported = overlay.rendered_image()

        # Logical row y=160, from x=100 to x=140: in the window's physical
        # pixels for the grab, and the crop's -- shifted by the selection's
        # origin first -- for the export.
        seen = self._red_width(
            on_screen, round(160 * ratio), round(100 * ratio), round(140 * ratio)
        )
        left, top = self.SELECTION.x(), self.SELECTION.y()
        saved = self._red_width(
            exported,
            round((160 - top) * ratio),
            round((100 - left) * ratio),
            round((140 - left) * ratio),
        )

        assert on_screen.devicePixelRatio() == ratio  # the grab really is physical
        assert abs(seen - stroke_width * ratio) <= 1
        assert abs(saved - seen) <= 1


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

    def test_committed_spotlight_dims_the_frame_immediately_after_release(self):
        frame = make_gradient_frame()
        overlay = OverlayWindow(frame)
        overlay.set_selection(QRect(0, 0, 200, 200))
        overlay._bar.select_tool("spotlight")

        QTest.mousePress(overlay, Qt.MouseButton.LeftButton, pos=QPoint(20, 20))
        QTest.mouseMove(overlay, QPoint(120, 120))
        QTest.mouseRelease(overlay, Qt.MouseButton.LeftButton, pos=QPoint(120, 120))

        # Outside the dragged rect, deep enough that neither the selection
        # frame nor a corner bracket (painted over the ink layer) could
        # account for the difference.
        raw = frame.image.pixelColor(170, 170)
        rendered = pixel(overlay.grab().toImage(), 170, 170)
        assert rendered != raw

    def test_two_committed_spotlights_both_stay_lit_on_screen(self):
        # The live-preview twin of test_shapes.py's
        # TestSpotlight.test_two_spotlights_both_stay_lit: `_base_layer_image`
        # has to make the same "combine, don't stack" call `render()` does,
        # or a second spotlight redims the first one's hole the moment it
        # is committed.
        frame = make_gradient_frame(size=(200, 200))
        overlay = OverlayWindow(frame)
        overlay.set_selection(QRect(0, 0, 200, 200))
        overlay.add_mark(
            Spotlight(colour=QColor("#ff0000"), stroke_width=4,
                      start=QPointF(10, 10), end=QPointF(50, 50))
        )
        overlay.add_mark(
            Spotlight(colour=QColor("#ff0000"), stroke_width=4,
                      start=QPointF(120, 120), end=QPointF(160, 160))
        )

        rendered = overlay.grab().toImage()

        assert pixel(rendered, 30, 30) == pixel(frame.image, 30, 30)
        assert pixel(rendered, 140, 140) == pixel(frame.image, 140, 140)
        assert pixel(rendered, 90, 90) != pixel(frame.image, 90, 90)

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
            # Ellipse: the bounding box's own left border, vertically
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
        ],
    )
    def test_click_with_eraser_active_removes_a_restored_tools_mark(self, mark, hit_point):
        # SNX-64 restored Ellipse and Line: the eraser reaches their marks
        # through the overlay, not only through shapes.py's own hit tests.
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


class TestEyedropperTool:
    """The eyedropper: `color_at` reads a pixel straight from `Frame.image`
    -- never a `grab()`/repaint of this widget, and never a logical point
    used as an image index directly, which is the coordinate-space bug a
    fractionally-scaled monitor (GNOME's common 1.5x) would otherwise hide.
    `pick_color_at` is `OverlayWindow.copy()`'s own shape -- clipboard, then
    toast -- but for one pixel's hex instead of the whole selection's image,
    and (per the ticket's "it creates no mark") never touches `_mark_store`.
    """

    MARKER = QColor(0, 255, 0)

    def _overlay(self, selection=QRect(0, 0, 200, 200)):
        frame = make_frame(image_size=(200, 200), logical_size=(200, 200))
        overlay = OverlayWindow(frame)
        overlay.set_selection(selection)
        return overlay

    def test_color_at_reads_the_frames_own_pixel_at_1x_scaling(self):
        overlay = self._overlay()
        overlay._frame.image.setPixelColor(40, 60, self.MARKER)

        assert overlay.color_at(QPointF(40, 60)) == self.MARKER

    def test_color_at_reads_the_correct_image_pixel_under_1_5x_scaling(self):
        # Image is 1.5x logical size -- GNOME's common fractional-scaling
        # case, and the one a naive "point used as an image index" bug
        # would misread: image-pixel (150, 150) is logical (100, 100) here,
        # not image-pixel (100, 100).
        image = QImage(300, 300, QImage.Format.Format_RGB32)
        image.fill(BASE_COLOR)
        image.setPixelColor(150, 150, self.MARKER)
        frame = Frame(image=image, logical_origin=QPointF(0, 0), logical_size=QSizeF(200, 200))
        overlay = OverlayWindow(frame)
        overlay.set_selection(QRect(0, 0, 200, 200))

        assert overlay.color_at(QPointF(100, 100)) == self.MARKER
        # The bug this guards against: image-pixel (100, 100) -- what a
        # naive "point used as an image index" read would return instead --
        # is still the base colour, never the marker.
        assert image.pixelColor(100, 100) == BASE_COLOR

    def test_click_copies_the_hex_to_the_clipboard_lowercase(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            output_module, "copy_text_to_clipboard", lambda text: calls.append(text)
        )
        overlay = self._overlay()
        overlay._frame.image.fill(QColor(0x3B, 0x82, 0xF6))  # the ticket's own #3b82f6
        overlay.set_eyedropper_active(True)

        QTest.mousePress(overlay, Qt.MouseButton.LeftButton, pos=QPoint(50, 50))

        assert calls == ["#3b82f6"]

    def test_click_with_eyedropper_inactive_copies_nothing(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            output_module, "copy_text_to_clipboard", lambda text: calls.append(text)
        )
        overlay = self._overlay()
        # set_eyedropper_active is never called: default state is inactive.

        QTest.mousePress(overlay, Qt.MouseButton.LeftButton, pos=QPoint(50, 50))

        assert calls == []

    def test_the_tool_stays_active_after_a_click(self, monkeypatch):
        monkeypatch.setattr(output_module, "copy_text_to_clipboard", lambda text: None)
        overlay = self._overlay()
        overlay.set_eyedropper_active(True)

        QTest.mousePress(overlay, Qt.MouseButton.LeftButton, pos=QPoint(50, 50))

        assert overlay._eyedropper_active is True

    def test_a_click_adds_nothing_to_the_undo_stack_or_marks(self, monkeypatch):
        monkeypatch.setattr(output_module, "copy_text_to_clipboard", lambda text: None)
        overlay = self._overlay()
        overlay.set_eyedropper_active(True)

        QTest.mousePress(overlay, Qt.MouseButton.LeftButton, pos=QPoint(50, 50))

        assert overlay.marks == ()
        assert overlay.can_undo is False

    def test_pick_color_at_never_calls_add_mark(self, monkeypatch):
        # Belt and suspenders on top of the undo-stack check above: nothing
        # here goes through the one path every real mark is added by.
        monkeypatch.setattr(output_module, "copy_text_to_clipboard", lambda text: None)
        calls = []
        overlay = self._overlay()
        monkeypatch.setattr(overlay, "add_mark", lambda mark: calls.append(mark))

        overlay.pick_color_at(QPointF(50, 50))

        assert calls == []

    def test_cursor_is_a_pointer_over_the_selection_while_the_eyedropper_is_active(self):
        overlay = self._overlay(selection=QRect(50, 50, 100, 80))
        overlay.set_eyedropper_active(True)
        overlay.show()
        QTest.qWaitForWindowExposed(overlay)

        QTest.mouseMove(overlay, QPoint(100, 90))  # deep inside the selection

        assert overlay.cursor().shape() == Qt.CursorShape.PointingHandCursor


def _send_move(widget, pos, buttons=Qt.MouseButton.NoButton):
    """A real move event, delivered straight to `widget` -- no dependence on
    which window the OS thinks is active, which QTest.mouseMove has on
    Windows even offscreen."""
    point = QPointF(pos)
    event = QMouseEvent(
        QEvent.Type.MouseMove,
        point,
        QPointF(widget.mapToGlobal(point.toPoint())),
        Qt.MouseButton.NoButton,
        buttons,
        Qt.KeyboardModifier.NoModifier,
    )
    QApplication.sendEvent(widget, event)


class TestEyedropperLoupeClearsTheChrome:
    """#106: the loupe and its hex chip open in the first corner around the
    pointer that clears the showing chrome and stays on the pointer's own
    monitor. All rects here are window-local logical, compared as rects --
    never sampled pixels.
    """

    MONITOR = QRectF(0, 0, 800, 800)

    def _overlay(self, monitors=None, size=(800, 800), selection=None):
        _close_stray_toplevel_windows()
        frame = make_frame(image_size=size, logical_size=size)
        overlay = OverlayWindow(frame, monitor_geometries=monitors or [self.MONITOR])
        overlay.setGeometry(0, 0, *size)
        overlay.show()
        QTest.qWaitForWindowExposed(overlay)
        overlay.set_selection(selection or QRect(100, 100, 600, 400))
        overlay.set_eyedropper_active(True)
        assert overlay._bar.isVisible()
        return overlay

    def _rects(self, overlay, cursor):
        return overlay._eyedropper_rects(QPointF(cursor), overlay.color_at(QPointF(cursor)))

    def test_just_above_the_bar_the_loupe_and_chip_clear_it(self):
        overlay = self._overlay()
        bar = QRectF(overlay._bar.geometry())
        cursor = QPointF(bar.left() + 30, overlay._selection.bottom() - 4)

        box, readout = self._rects(overlay, cursor)

        assert not box.intersects(bar)
        assert not readout.intersects(bar)
        assert self.MONITOR.contains(box) and self.MONITOR.contains(readout)

    def test_the_default_corner_is_still_below_right(self):
        overlay = self._overlay()
        cursor = QPointF(200, 150)

        box, readout = self._rects(overlay, cursor)

        assert box.topLeft() == cursor + Overlay.MAGNIFIER_OFFSET
        assert readout.top() == box.bottom() + overlay._EYEDROPPER_READOUT_GAP
        assert readout.left() == box.left()

    def test_near_the_monitors_right_edge_it_flips_left(self):
        overlay = self._overlay(selection=QRect(100, 100, 690, 300))
        cursor = QPointF(780, 150)

        box, readout = self._rects(overlay, cursor)

        assert box.right() == cursor.x() - Overlay.MAGNIFIER_OFFSET.x()
        assert box.top() > cursor.y()
        assert self.MONITOR.contains(readout)

    def test_near_the_monitors_bottom_it_flips_above_with_the_chip_on_top(self):
        overlay = self._overlay(selection=QRect(100, 450, 300, 345))
        cursor = QPointF(200, 790)

        box, readout = self._rects(overlay, cursor)

        assert box.bottom() == cursor.y() - Overlay.MAGNIFIER_OFFSET.y()
        assert readout.bottom() == box.top() - overlay._EYEDROPPER_READOUT_GAP
        assert self.MONITOR.contains(box) and self.MONITOR.contains(readout)

    def test_on_a_two_monitor_desk_it_stays_on_the_pointers_monitor(self):
        left, right = QRectF(0, 0, 800, 800), QRectF(800, 0, 800, 800)
        overlay = self._overlay(
            monitors=[left, right], size=(1600, 800), selection=QRect(100, 100, 1400, 300)
        )
        # The window runs on past the bezel, so a window clamp would let the
        # box hang across it. Only the monitor keeps it on the left one.
        cursor = QPointF(790, 150)

        box, readout = self._rects(overlay, cursor)

        assert left.contains(box)
        assert left.contains(readout)

    def test_with_no_clear_corner_it_goes_above_left_inside_the_monitor(self, monkeypatch):
        overlay = self._overlay()
        monkeypatch.setattr(overlay, "_chrome_to_keep_clear", lambda: [QRectF(self.MONITOR)])
        cursor = QPointF(60, 60)

        box, _readout = self._rects(overlay, cursor)

        assert self.MONITOR.contains(box)
        assert box.topLeft() == QPointF(0, 0)

    def test_an_open_style_popover_is_kept_clear_too(self):
        overlay = self._overlay()
        overlay._toggle_style()
        popover = QRectF(overlay._style_popover.geometry())
        assert overlay._style_popover.isVisible()
        cursor = QPointF(popover.left() + 10, popover.top() - 30)

        box, readout = self._rects(overlay, cursor)

        assert not box.intersects(popover)
        assert not readout.intersects(popover)


class TestChromeFadesWhileAToolWorksUnderIt:
    """The bar and the tool hint fade to `WORKING_OPACITY` while a tool is
    working under them, come back when it stops or the pointer reaches
    them, and never reach an export either way.
    """

    SIZE = (800, 800)

    def _overlay(self):
        # The selection is the whole monitor, so the bar sits inside it and
        # a tool can work right up against it.
        _close_stray_toplevel_windows()
        frame = make_frame(image_size=self.SIZE, logical_size=self.SIZE)
        overlay = OverlayWindow(frame, monitor_geometries=[QRectF(0, 0, *self.SIZE)])
        overlay.setGeometry(0, 0, *self.SIZE)
        overlay.show()
        QTest.qWaitForWindowExposed(overlay)
        overlay.set_selection(overlay.rect())
        assert overlay._bar.isVisible()
        return overlay

    def _just_above(self, widget) -> QPoint:
        geometry = widget.geometry()
        return QPoint(geometry.center().x(), geometry.top() - 4)

    def test_the_eyedropper_reading_beside_the_bar_fades_it(self):
        overlay = self._overlay()
        overlay.set_eyedropper_active(True)

        _send_move(overlay, self._just_above(overlay._bar))

        assert overlay.is_chrome_faded(overlay._bar)
        effect = overlay._bar.graphicsEffect()
        assert effect.opacity() == tokens.BarMetric.WORKING_OPACITY

    def test_moving_away_brings_the_bar_back(self):
        overlay = self._overlay()
        overlay.set_eyedropper_active(True)
        _send_move(overlay, self._just_above(overlay._bar))

        _send_move(overlay, QPoint(100, 100))

        assert not overlay.is_chrome_faded(overlay._bar)
        assert overlay._bar.graphicsEffect() is None

    def test_disarming_the_eyedropper_brings_the_bar_back(self):
        overlay = self._overlay()
        overlay.set_eyedropper_active(True)
        _send_move(overlay, self._just_above(overlay._bar))

        overlay.set_eyedropper_active(False)

        assert not overlay.is_chrome_faded(overlay._bar)

    def test_a_hover_elsewhere_leaves_the_bar_alone(self):
        overlay = self._overlay()
        overlay.set_eyedropper_active(True)

        _send_move(overlay, QPoint(100, 100))

        assert not overlay.is_chrome_faded(overlay._bar)

    def test_no_tool_means_no_fade(self):
        overlay = self._overlay()

        _send_move(overlay, self._just_above(overlay._bar))

        assert not overlay.is_chrome_faded(overlay._bar)

    def test_reaching_the_bar_brings_it_back_so_it_can_be_clicked(self):
        overlay = self._overlay()
        overlay.set_eyedropper_active(True)
        _send_move(overlay, self._just_above(overlay._bar))
        assert overlay.is_chrome_faded(overlay._bar)

        QApplication.sendEvent(overlay._bar, QEvent(QEvent.Type.Enter))

        assert not overlay.is_chrome_faded(overlay._bar)

    def test_a_stroke_passing_under_the_bar_fades_it_until_release(self):
        overlay = self._overlay()
        overlay._bar.select_tool("pen")
        bar = overlay._bar.geometry()
        start = QPoint(bar.center().x(), bar.top() - 120)

        QTest.mousePress(overlay, Qt.MouseButton.LeftButton, pos=start)
        _send_move(overlay, start + QPoint(0, 40), Qt.MouseButton.LeftButton)
        assert not overlay.is_chrome_faded(overlay._bar)
        _send_move(overlay, bar.center(), Qt.MouseButton.LeftButton)
        assert overlay.is_chrome_faded(overlay._bar)

        QTest.mouseRelease(overlay, Qt.MouseButton.LeftButton, pos=bar.center())

        assert not overlay.is_chrome_faded(overlay._bar)
        assert len(overlay.marks) == 1

    def test_the_tool_hint_fades_too(self):
        overlay = self._overlay()
        overlay._bar.select_tool("pen")
        hint = overlay._tool_hint
        assert hint.isVisible()
        start = QPoint(hint.geometry().center().x(), hint.geometry().top() - 150)

        QTest.mousePress(overlay, Qt.MouseButton.LeftButton, pos=start)
        _send_move(overlay, hint.geometry().center(), Qt.MouseButton.LeftButton)

        assert overlay.is_chrome_faded(hint)
        QTest.mouseRelease(overlay, Qt.MouseButton.LeftButton, pos=hint.geometry().center())
        assert not overlay.is_chrome_faded(hint)

    def test_a_pick_under_the_bar_reads_the_frame_not_the_bar(self, monkeypatch):
        overlay = self._overlay()
        overlay.set_eyedropper_active(True)
        under = QPointF(overlay._bar.geometry().center())
        marker = QColor(0x12, 0x34, 0x56)
        scale_x, scale_y = overlay._window_to_frame_scale()
        overlay._frame.image.setPixelColor(
            int(under.x() * scale_x), int(under.y() * scale_y), marker
        )
        calls = []
        monkeypatch.setattr(output_module, "copy_text_to_clipboard", calls.append)

        overlay.pick_color_at(under)

        assert calls == ["#123456"]

    def test_the_export_holds_no_chrome_faded_or_not(self):
        overlay = self._overlay()
        overlay.set_eyedropper_active(True)
        bar = overlay._bar.geometry()
        scale_x, scale_y = overlay._window_to_frame_scale()

        def bar_pixels(image):
            return {
                image.pixel(int(x * scale_x), int(y * scale_y))
                for x in range(bar.left(), bar.right(), 7)
                for y in range(bar.top(), bar.bottom(), 5)
            }

        assert bar_pixels(overlay.rendered_image()) == {BASE_COLOR}
        _send_move(overlay, self._just_above(overlay._bar))
        assert overlay.is_chrome_faded(overlay._bar)
        assert bar_pixels(overlay.rendered_image()) == {BASE_COLOR}


class TestDrawingTools:
    """SNX-52: a press inside the selection starts a mark for whichever
    tool `_bar.active_tool` names; move extends it; release either commits
    it (via shapes.finalize_mark) or discards it if it never reached the
    spec's minimum size. A committed mark takes its colour, stroke,
    fill, line style and -- for a redaction -- strength from the active
    tool's own style (`_styles`), and its shape class from the tool, per
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
        # Solid: the rectangle's seed is dashed, and a dash gap could fall
        # on the pixel sampled.
        overlay._styles.update("rect", colour="#ff0000", size=6, dash="solid", fill="outline")

        QTest.mousePress(overlay, Qt.MouseButton.LeftButton, pos=QPoint(20, 20))
        QTest.mouseMove(overlay, QPoint(80, 60))

        rendered = overlay.grab().toImage()
        assert pixel(rendered, 20, 40) == QColor("#ff0000")  # left edge of the live preview

        QTest.mouseRelease(overlay, Qt.MouseButton.LeftButton, pos=QPoint(80, 60))

    def test_ellipse_press_move_release_commits_from_press_to_release(self):
        # SNX-64: restored via `_TWO_POINT_MARK_CLASSES`, reached through
        # the shapes family -- see TestFamilyMenuOverlayIntegration -- but the
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

    @pytest.mark.parametrize("tool", ["ellipse", "line"])
    def test_restored_shape_tools_take_colour_and_stroke_from_their_own_style(self, tool):
        overlay = self._overlay()
        overlay._bar.select_tool(tool)
        overlay._styles.update(tool, colour="#38bdf8", size=9)

        QTest.mousePress(overlay, Qt.MouseButton.LeftButton, pos=QPoint(20, 20))
        QTest.mouseMove(overlay, QPoint(80, 60))
        QTest.mouseRelease(overlay, Qt.MouseButton.LeftButton, pos=QPoint(80, 60))

        mark = overlay.marks[0]
        assert mark.colour == QColor("#38bdf8")
        assert mark.stroke_width == 9

    def test_blur_press_move_release_commits_a_blur_shape(self):
        overlay = self._overlay()
        overlay._bar.select_tool("blur")
        overlay._styles.update("blur", strength=12)

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

    def test_the_pixelate_tool_commits_a_pixelate_shape(self):
        overlay = self._overlay()
        overlay._bar.select_tool("pixelate")

        QTest.mousePress(overlay, Qt.MouseButton.LeftButton, pos=QPoint(20, 20))
        QTest.mouseMove(overlay, QPoint(80, 60))
        QTest.mouseRelease(overlay, Qt.MouseButton.LeftButton, pos=QPoint(80, 60))

        assert isinstance(overlay.marks[0], Pixelate)

    def test_the_blackout_tool_commits_a_blackout(self):
        overlay = self._overlay()
        overlay._bar.select_tool("blackout")

        QTest.mousePress(overlay, Qt.MouseButton.LeftButton, pos=QPoint(20, 20))
        QTest.mouseMove(overlay, QPoint(80, 60))
        QTest.mouseRelease(overlay, Qt.MouseButton.LeftButton, pos=QPoint(80, 60))

        assert type(overlay.marks[0]).__name__ == "Blackout"

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
        overlay._styles.update("text", colour="#123456", size=5)

        QTest.mousePress(overlay, Qt.MouseButton.LeftButton, pos=QPoint(30, 30))
        QApplication.processEvents()
        QTest.keyClicks(overlay._text_edit, "first")

        # Changed before the second click, so the assertions below can tell
        # apart "the first label kept its own colour/size" from "it silently
        # picked up whatever the text tool's style holds at commit time."
        overlay._styles.update("text", colour="#abcdef", size=12)

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

    def test_callout_drag_opens_the_label_editor_with_no_mark_yet(self):
        # Unlike Rectangle/Arrow, a callout's drag alone does not commit --
        # see Callout's own docstring for why body/tail/text have to land
        # in the store together, as one undo step.
        overlay = self._overlay()
        overlay.show()
        QTest.qWaitForWindowExposed(overlay)
        overlay._bar.select_tool("callout")

        QTest.mousePress(overlay, Qt.MouseButton.LeftButton, pos=QPoint(20, 20))
        QTest.mouseMove(overlay, QPoint(80, 60))
        QTest.mouseRelease(overlay, Qt.MouseButton.LeftButton, pos=QPoint(80, 60))
        QApplication.processEvents()

        assert overlay.marks == ()
        assert overlay._text_edit is not None
        assert not overlay._text_edit.isHidden()

    def test_callout_commits_once_typed_and_editing_finishes(self):
        overlay = self._overlay()
        overlay.show()
        QTest.qWaitForWindowExposed(overlay)
        overlay._bar.select_tool("callout")

        QTest.mousePress(overlay, Qt.MouseButton.LeftButton, pos=QPoint(20, 20))
        QTest.mouseMove(overlay, QPoint(80, 60))
        QTest.mouseRelease(overlay, Qt.MouseButton.LeftButton, pos=QPoint(80, 60))
        QApplication.processEvents()
        QTest.keyClicks(overlay._text_edit, "Click here")
        QTest.keyClick(overlay._text_edit, Qt.Key.Key_Return)

        assert len(overlay.marks) == 1
        mark = overlay.marks[0]
        assert isinstance(mark, Callout)
        assert mark.text == "Click here"
        assert mark.start == QPointF(20, 20)  # the drag's own start -- the tail's tip
        assert mark.end == QPointF(80, 60)
        assert overlay._text_edit.isHidden()

    def test_callout_with_nothing_typed_still_commits_body_and_tail(self):
        # Unlike a bare Text label, an empty callout is still a real mark:
        # the drag already placed a visible body and tail, so committing it
        # is not conditioned on typing anything into it.
        overlay = self._overlay()
        overlay.show()
        QTest.qWaitForWindowExposed(overlay)
        overlay._bar.select_tool("callout")

        QTest.mousePress(overlay, Qt.MouseButton.LeftButton, pos=QPoint(20, 20))
        QTest.mouseMove(overlay, QPoint(80, 60))
        QTest.mouseRelease(overlay, Qt.MouseButton.LeftButton, pos=QPoint(80, 60))
        QApplication.processEvents()
        QTest.keyClick(overlay._text_edit, Qt.Key.Key_Return)

        assert len(overlay.marks) == 1
        mark = overlay.marks[0]
        assert isinstance(mark, Callout)
        assert mark.text == ""

    def test_callout_abandon_discards_the_half_typed_word_not_the_mark(self):
        # SNX-79's Escape-abandons-a-label rule, applied to Callout: the
        # word being typed is discarded, but the body and tail the drag
        # already placed are not -- see Callout's own docstring for why
        # this differs from a bare Text label, which never commits at all
        # without a click landing outside it first.
        overlay = self._overlay()
        overlay.show()
        QTest.qWaitForWindowExposed(overlay)
        overlay._bar.select_tool("callout")

        QTest.mousePress(overlay, Qt.MouseButton.LeftButton, pos=QPoint(20, 20))
        QTest.mouseMove(overlay, QPoint(80, 60))
        QTest.mouseRelease(overlay, Qt.MouseButton.LeftButton, pos=QPoint(80, 60))
        QApplication.processEvents()
        QTest.keyClicks(overlay._text_edit, "a draft nobody wants")

        QTest.keyClick(overlay, Qt.Key.Key_Escape)

        assert len(overlay.marks) == 1
        assert overlay.marks[0].text == ""

    def test_one_undo_removes_the_whole_callout(self):
        # The whole reason a callout beats an arrow plus a separate label:
        # body, tail and text are one mark, so one undo takes all three.
        overlay = self._overlay()
        overlay.show()
        QTest.qWaitForWindowExposed(overlay)
        overlay._bar.select_tool("callout")

        QTest.mousePress(overlay, Qt.MouseButton.LeftButton, pos=QPoint(20, 20))
        QTest.mouseMove(overlay, QPoint(80, 60))
        QTest.mouseRelease(overlay, Qt.MouseButton.LeftButton, pos=QPoint(80, 60))
        QApplication.processEvents()
        QTest.keyClicks(overlay._text_edit, "hello")
        QTest.keyClick(overlay._text_edit, Qt.Key.Key_Return)
        assert len(overlay.marks) == 1

        overlay.undo()

        assert overlay.marks == ()

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

    def test_committed_mark_takes_the_tools_own_colour_and_stroke(self):
        overlay = self._overlay()
        overlay._bar.select_tool("pen")
        overlay._styles.update("pen", colour="#123456", size=17)

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
    point Window and Full screen each already have. Before this,
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
            output_module, "copy_image_to_clipboard", lambda image: calls.append(image)
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
            output_module, "copy_image_to_clipboard", lambda image: calls.append(image)
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
    """The stills bar is one row: the split action, a divider, Copy text, a
    divider, eight tool slots, a divider, the style dot, a divider, then
    undo and clear -- docs/design/bars/README.md section 2, with Copy text
    added past it (#82; see FloatingBar's own docstring). Built from real
    widgets rather than painted, so tooltips and hover come for free
    (TestFloatingBarTooltips below covers the tooltip half).
    """

    @staticmethod
    def _laid_out(bar: FloatingBar) -> FloatingBar:
        bar.resize(bar.sizeHint())
        bar.grab()
        return bar

    def test_contains_one_button_per_control_in_the_spec_table(self):
        bar = FloatingBar()

        buttons = bar.findChildren(QPushButton)

        # 8 slots + Copy text + the watermark + undo + clear == 12 on the
        # overlay's bar. The destinations are one split action and the
        # style dot is a widget of its own, neither a QPushButton; the mode
        # chip and redo are built but not placed, since the handoff's
        # post-selection bar carries neither.
        visible = [button for button in buttons if not button.isHidden()]
        assert len(visible) == 12
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
        # No fresh selection to read text out of -- see FloatingBar's own
        # docstring on why Copy text is absent here.
        assert bar._copy_text_button is None

    def test_slots_run_in_the_handoffs_order(self):
        # A gradient of consequence, never reordered: muscle memory is the
        # feature.
        bar = FloatingBar()

        assert list(bar._tool_buttons) == [
            "pen", "highlighter", "shapes", "step", "text", "redact", "eraser", "eyedropper"
        ]

    def test_the_row_reads_action_tools_style_then_history(self):
        bar = self._laid_out(FloatingBar())
        dividers = sorted(bar.findChildren(_Divider), key=lambda d: d.geometry().x())
        row = [
            bar._action,
            dividers[0],
            bar._copy_text_button,
            dividers[1],
            *bar._tool_buttons.values(),
            dividers[2],
            bar._style_dot,
            bar._watermark,
            dividers[3],
            bar._undo_button,
            bar._clear_button,
        ]

        lefts = [widget.geometry().x() for widget in row]

        assert len(dividers) == 4
        assert lefts == sorted(set(lefts))

    def test_the_review_windows_bar_adds_only_its_chip_and_redo(self):
        bar = FloatingBar(trailing="done")

        assert len(bar.findChildren(_Divider)) == 4
        assert not bar._redo_button.isHidden()

    def test_the_bar_is_one_row_of_its_token_height(self):
        bar = self._laid_out(FloatingBar())

        # Device-independent: at 1.5 scaling a correct row grabs as 63
        # device pixels.
        assert bar.grab().deviceIndependentSize().height() == tokens.BarMetric.ROW_H

    def test_the_split_action_is_the_only_accent_filled_control(self):
        bar = self._laid_out(FloatingBar())
        image = bar.grab().toImage()
        ratio = image.devicePixelRatio()
        accent = QColor(tokens.BarColor.ACCENT)
        action = QRectF(bar._action.geometry()).adjusted(-1, -1, 1, 1)

        stray = [
            (x, y)
            for y in range(image.height())
            for x in range(image.width())
            if image.pixel(x, y) == accent.rgb()
            and not action.contains(QPointF(x / ratio, y / ratio))
        ]

        face = pixel(image, bar._action.geometry().left() + 4, bar._action.geometry().center().y())
        assert face == accent
        assert stray == []

    def test_picking_a_tool_never_moves_anything_to_its_left(self):
        bar = self._laid_out(FloatingBar())
        before = bar._action.geometry()

        for tool in ("arrow", "blackout", "eraser"):
            bar.select_tool(tool)
            self._laid_out(bar)

            assert bar._action.geometry() == before, tool

    def test_a_notch_is_on_the_family_slots_and_the_watermark_and_nowhere_else(self):
        # A notch means this slot has more: a family's other siblings, or
        # where the watermark goes and how strongly.
        bar = FloatingBar()

        notched = [slot for slot, button in bar._tool_buttons.items() if button.notch is not None]

        assert notched == ["shapes", "redact"]
        assert bar._watermark.notch is not None
        assert len(bar.findChildren(overlay_module._Notch)) == 3

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

    def test_background_pixel_is_painted_at_the_fallback_alpha(self):
        # Built with no overlay behind it, the bar has no frame to blur, so
        # its fill rises to the fallback's alpha (#70). The token's 94% over
        # a blur of the frame is in test_glass.py.
        bar = FloatingBar()
        bar.resize(bar.sizeHint())

        rendered = bar.grab().toImage()
        # Top padding strip, mid-width: inside the rounded fill but above
        # every button, so this is background only.
        sampled = pixel(rendered, bar.width() // 2, 2)

        expected_alpha = round(tokens.BarColor.FALLBACK_BG_ALPHA * 255)
        assert sampled.alpha() == pytest.approx(expected_alpha, abs=2)
        assert (sampled.red(), sampled.green(), sampled.blue()) == QColor(
            tokens.BarColor.BAR_BG
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
    """Centred on the selection and 16px below it, at least 12px inside the
    selection's monitor, and above the selection when below does not fit
    -- docs/design/bars/divergences.md 8, rather than the handoff's clamp.
    """

    MARGIN = tokens.BarMetric.BAR_EDGE_MARGIN
    OFFSET = tokens.BarMetric.BAR_OFFSET_Y

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
        expected_top = QRectF(selection).bottom() + self.OFFSET
        assert bar.geometry().top() == expected_top

    def test_no_room_below_flips_the_bar_above_the_selection(self):
        # The natural position would land past the monitor's bottom edge.
        # This used to clamp the bar upward, which put it *on top of the
        # selection* -- covering the pixels the user framed in order to
        # annotate them. Reported as "when u select a small region the
        # controls are in the region so u cant edit anything".
        bounds = QRectF(0, 0, 1600, 400)
        selection = QRect(400, 350, 200, 40)
        bar = FloatingBar()

        bar.reposition(selection, bounds)

        assert QRectF(bar.geometry()).bottom() == QRectF(selection).top() - self.OFFSET
        assert bounds.contains(QRectF(bar.geometry()))

    def test_breaking_the_margin_below_counts_as_no_room(self):
        # 16px below this selection is still on the monitor, but not 12px
        # inside it.
        bounds = QRectF(0, 0, 1600, 1000)
        selection = QRect(400, 900, 200, 40)
        bar = FloatingBar()

        bar.reposition(selection, bounds)

        assert QRectF(bar.geometry()).bottom() == QRectF(selection).top() - self.OFFSET

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
        # bottom edge is.
        bounds = QRectF(0, 0, 2560, 1440)
        selection = QRect(700, 300, 1123, 1104)  # bottom at 1404
        bar = FloatingBar()

        bar.reposition(selection, bounds)

        assert not QRectF(bar.geometry()).intersects(QRectF(selection))

    def test_a_selection_filling_the_monitor_still_lands_inside_its_margins(self):
        # Neither side has room, so overlap is unavoidable -- but the bar
        # must still be somewhere the user can see and press it.
        bounds = QRectF(0, 0, 1600, 400)
        selection = QRect(0, 0, 1600, 400)
        bar = FloatingBar()

        bar.reposition(selection, bounds)

        inside = bounds.adjusted(self.MARGIN, self.MARGIN, -self.MARGIN, -self.MARGIN)
        assert inside.contains(QRectF(bar.geometry()))

    def test_room_below_is_still_preferred(self):
        bounds = QRectF(0, 0, 1600, 1000)
        selection = QRect(400, 200, 200, 100)
        bar = FloatingBar()

        bar.reposition(selection, bounds)

        expected = QRectF(selection).bottom() + self.OFFSET
        assert bar.geometry().top() == expected

    def test_it_stays_its_margin_inside_the_monitors_left_edge(self):
        bar = FloatingBar()
        bounds = QRectF(0, 0, 1600, 1000)

        bar.reposition(QRect(0, 200, 50, 50), bounds)

        assert bar.geometry().left() == bounds.left() + self.MARGIN

    def test_it_stays_its_margin_inside_the_monitors_right_edge(self):
        bar = FloatingBar()
        bounds = QRectF(0, 0, 1600, 1000)

        bar.reposition(QRect(1580, 200, 15, 50), bounds)

        assert QRectF(bar.geometry()).right() == bounds.right() - self.MARGIN

    def test_on_a_monitor_with_a_negative_origin(self):
        # Left of and above the primary, so every coordinate on it is
        # negative and none of the arithmetic may lean on a zero edge.
        bounds = QRectF(-1920, -1080, 1920, 1080)
        bar = FloatingBar()

        bar.reposition(QRect(-1200, -900, 400, 300), bounds)
        geometry = QRectF(bar.geometry())
        assert geometry.center().x() == pytest.approx(-1000, abs=1)
        assert geometry.top() == -600 + self.OFFSET

        bar.reposition(QRect(-1915, -900, 40, 40), bounds)
        assert bar.geometry().left() == -1920 + self.MARGIN

        # Bottom at -40: no room below, so above.
        bar.reposition(QRect(-1200, -300, 400, 260), bounds)
        geometry = QRectF(bar.geometry())
        assert geometry.bottom() == -300 - self.OFFSET
        assert bounds.contains(geometry)

    def test_through_the_overlay_on_a_monitor_left_of_the_origin(self):
        _close_stray_toplevel_windows()
        left, right = QRectF(-1920, 0, 1920, 1080), QRectF(0, 0, 1920, 1080)
        frame = make_frame(
            image_size=(3840, 1080), logical_size=(3840, 1080), logical_origin=(-1920, 0)
        )
        overlay = OverlayWindow(frame, monitor_geometries=[left, right])
        overlay.show()
        QTest.qWaitForWindowExposed(overlay)

        # Window coordinates run from the desktop's own left edge, so x=5
        # is hard against the left monitor's left edge.
        overlay.set_selection(QRect(5, 300, 60, 200))

        bar = QRectF(overlay._bar.geometry())
        assert bar.left() == self.MARGIN
        assert bar.top() == 500 + self.OFFSET
        assert QRectF(0, 0, 1920, 1080).contains(bar)


# ---------------------------------------------------------------------------
# Dragging the bar (#50)
# ---------------------------------------------------------------------------

_BAR_POINTER_EVENTS = {
    "press": (QEvent.Type.MouseButtonPress, Qt.MouseButton.LeftButton),
    "move": (QEvent.Type.MouseMove, Qt.MouseButton.NoButton),
    "release": (QEvent.Type.MouseButtonRelease, Qt.MouseButton.LeftButton),
}


def _bar_pointer(widget: QWidget, kind: str, local: QPointF, buttons) -> None:
    """Deliver one pointer event to `widget` at `local`, its own logical
    coordinates. Through `QApplication.sendEvent`, so a press `widget`
    declines is handed on to its parent the way Qt hands one on.
    """
    event_type, button = _BAR_POINTER_EVENTS[kind]
    QApplication.sendEvent(
        widget,
        QMouseEvent(
            event_type,
            QPointF(local),
            widget.mapToGlobal(QPointF(local)),
            button,
            buttons,
            Qt.KeyboardModifier.NoModifier,
        ),
    )


def _grip(bar: FloatingBar) -> QPointF:
    """A point on the bar's own surface -- its top padding, halfway along."""
    return QPointF(bar.width() / 2, 2)


def _drag_bar(
    bar: FloatingBar, travel: QPointF, *, at: QPointF | None = None, release: bool = True
) -> None:
    """Press on `bar` at `at` (its own coordinates; its grip by default),
    move the pointer `travel` in the parent's coordinates in two steps, and
    release.

    Each move is mapped against wherever the bar has got to by then, as Qt
    maps a grabbed drag's moves: the bar moves under the pointer, so a fixed
    point in the bar's own coordinates would be a pointer moving with it.
    """
    left = Qt.MouseButton.LeftButton
    start = at if at is not None else _grip(bar)
    start_in_parent = bar.mapToParent(start)
    _bar_pointer(bar, "press", start, left)
    for step in (0.5, 1.0):
        _bar_pointer(bar, "move", bar.mapFromParent(start_in_parent + travel * step), left)
    if release:
        _bar_pointer(
            bar, "release", bar.mapFromParent(start_in_parent + travel), Qt.MouseButton.NoButton
        )


class TestTheBarDrags:
    """The stills bar is dragged by its own surface -- the padding, the gaps
    between controls and the dividers -- and never by a control on it.
    """

    BOUNDS = QRectF(0, 0, 1600, 1000)
    SELECTION = QRect(400, 200, 200, 150)
    MARGIN = tokens.BarMetric.BAR_EDGE_MARGIN
    OFFSET = tokens.BarMetric.BAR_OFFSET_Y
    LEFT = Qt.MouseButton.LeftButton

    def _bar(self) -> FloatingBar:
        bar = FloatingBar()
        bar.reposition(self.SELECTION, self.BOUNDS)
        # Laid out now: an unshown widget defers its layout, and `childAt`
        # is what tells a grip from a control.
        bar.layout().activate()
        return bar

    def test_the_grip_these_tests_use_is_the_bars_own_surface(self):
        bar = self._bar()

        assert bar.childAt(_grip(bar).toPoint()) is None

    def test_a_drag_on_its_own_surface_moves_it(self):
        bar = self._bar()
        before = bar.pos()

        _drag_bar(bar, QPointF(-150, 300))

        assert bar.pos() == before + QPoint(-150, 300)

    def test_a_divider_is_a_grip_too(self):
        bar = self._bar()
        divider = bar.findChildren(_Divider)[0]
        before = bar.pos()

        _drag_bar(bar, QPointF(0, 200), at=QPointF(divider.geometry().center()))

        assert bar.pos() == before + QPoint(0, 200)

    def test_a_press_that_barely_moves_leaves_it_where_it_was(self):
        # A press on the padding is usually a click that missed a button.
        bar = self._bar()
        before = bar.pos()
        started = []
        bar.dragStarted.connect(lambda: started.append(True))

        _drag_bar(bar, QPointF(QApplication.startDragDistance() / 3, 0))

        assert bar.pos() == before
        assert started == []

    def test_a_press_on_a_button_is_still_a_click(self):
        bar = self._bar()
        before = bar.pos()
        picked = []
        bar.toolSelected.connect(picked.append)
        button = bar._tool_buttons["text"]
        centre = QPointF(button.width() / 2, button.height() / 2)
        # Past the drag distance, and still inside the button.
        slid = centre + QPointF(QApplication.startDragDistance() + 1, 0)
        assert button.rect().contains(slid.toPoint())

        _bar_pointer(button, "press", centre, self.LEFT)
        _bar_pointer(button, "move", slid, self.LEFT)
        _bar_pointer(button, "release", slid, Qt.MouseButton.NoButton)

        assert picked == ["text"]
        assert bar.pos() == before
        assert not bar.is_dragging

    def test_no_control_on_the_row_is_a_grip(self):
        bar = self._bar()
        controls = [
            *bar._tool_buttons.values(),
            *(button.notch for button in bar._tool_buttons.values() if button.notch),
            bar._style_dot,
            bar._watermark,
            bar._watermark.notch,
            bar._action,
            bar._undo_button,
            bar._clear_button,
        ]

        for control in controls:
            centre = control.mapTo(bar, control.rect().center())
            assert not bar._is_grip(QPointF(centre)), control

    def test_a_control_that_declines_its_press_is_still_not_a_grip(self):
        # A label or a plainly painted widget declines every press, and Qt
        # hands it on to the bar. A slot built from one must not become a
        # grip in the middle of the row.
        bar = self._bar()
        slot = bar._tool_buttons["step"]
        label = QLabel("slot", bar)
        label.setGeometry(slot.geometry())
        label.show()
        label.raise_()
        before = bar.pos()
        centre = QPointF(label.rect().center())

        _bar_pointer(label, "press", centre, self.LEFT)
        _bar_pointer(label, "move", centre + QPointF(0, 200), self.LEFT)
        _bar_pointer(label, "release", centre + QPointF(0, 200), Qt.MouseButton.NoButton)

        assert bar.pos() == before
        assert not bar.is_dragging

    def test_a_disabled_button_is_not_a_grip_either(self):
        # Undo with nothing to undo sits in the middle of the row. Qt's
        # buttons keep a press while disabled rather than let it through.
        bar = self._bar()
        undo = bar._undo_button
        assert not undo.isEnabled()
        before = bar.pos()
        centre = QPointF(undo.rect().center())

        _bar_pointer(undo, "press", centre, self.LEFT)
        _bar_pointer(undo, "move", centre + QPointF(0, 200), self.LEFT)
        _bar_pointer(undo, "release", centre + QPointF(0, 200), Qt.MouseButton.NoButton)

        assert bar.pos() == before
        assert not bar.is_dragging

    def test_it_stays_inside_its_bounds_however_far_it_is_dragged(self):
        bar = self._bar()
        inside = self.BOUNDS.adjusted(self.MARGIN, self.MARGIN, -self.MARGIN, -self.MARGIN)

        _drag_bar(bar, QPointF(5000, 5000))
        assert QRectF(bar.geometry()).bottomRight() == inside.bottomRight()

        _drag_bar(bar, QPointF(-9000, -9000))
        assert QRectF(bar.geometry()).topLeft() == inside.topLeft()

    def test_the_cursor_says_the_surface_can_be_taken_hold_of(self):
        bar = FloatingBar()
        assert bar.cursor().shape() != Qt.CursorShape.OpenHandCursor

        bar.reposition(self.SELECTION, self.BOUNDS)
        bar.layout().activate()
        assert bar.cursor().shape() == Qt.CursorShape.OpenHandCursor
        assert bar._tool_buttons["pen"].cursor().shape() == Qt.CursorShape.PointingHandCursor

        _drag_bar(bar, QPointF(0, 200), release=False)
        assert bar.cursor().shape() == Qt.CursorShape.ClosedHandCursor

        _bar_pointer(bar, "release", _grip(bar), Qt.MouseButton.NoButton)
        assert bar.cursor().shape() == Qt.CursorShape.OpenHandCursor

    def test_a_re_sync_for_the_same_selection_keeps_it_where_it_was_dragged(self):
        bar = self._bar()
        _drag_bar(bar, QPointF(-300, 200))
        dragged = bar.pos()

        bar.reposition(QRect(self.SELECTION), self.BOUNDS)

        assert bar.pos() == dragged

    def test_a_new_selection_puts_it_beside_that_selection_again(self):
        bar = self._bar()
        _drag_bar(bar, QPointF(-300, 200))
        reframed = QRect(400, 200, 200, 160)

        bar.reposition(reframed, self.BOUNDS)

        assert QRectF(bar.geometry()).top() == QRectF(reframed).bottom() + self.OFFSET


class TestTheBarsSpot:
    """A position is remembered as a fraction of the room the bar can travel
    inside its bounds, so it names the same place on any monitor.
    """

    SIZE = QSize(448, 42)
    MARGIN = tokens.BarMetric.BAR_EDGE_MARGIN

    def _inside(self, bounds: QRectF) -> QRectF:
        return bounds.adjusted(self.MARGIN, self.MARGIN, -self.MARGIN, -self.MARGIN)

    def test_the_corners_are_the_ends_of_its_travel(self):
        bounds = QRectF(0, 0, 1600, 1000)
        inside = self._inside(bounds)

        top_left = FloatingBar.from_spot((0.0, 0.0), bounds, self.SIZE)
        bottom_right = FloatingBar.from_spot((1.0, 1.0), bounds, self.SIZE)

        assert QPointF(top_left) == inside.topLeft()
        assert QRectF(QRect(bottom_right, self.SIZE)).bottomRight() == inside.bottomRight()

    def test_a_position_round_trips(self):
        bounds = QRectF(0, 0, 1600, 1000)

        spot = FloatingBar.spot(QPointF(300, 700), bounds, self.SIZE)

        assert FloatingBar.from_spot(spot, bounds, self.SIZE) == QPoint(300, 700)

    def test_a_corner_is_the_same_corner_on_a_monitor_of_another_size(self):
        spot = FloatingBar.spot(
            QPointF(2560 - self.MARGIN - self.SIZE.width(), self.MARGIN),
            QRectF(0, 0, 2560, 1440),
            self.SIZE,
        )
        smaller = QRectF(0, 0, 1280, 800)

        placed = QRectF(QRect(FloatingBar.from_spot(spot, smaller, self.SIZE), self.SIZE))

        assert placed.topRight() == self._inside(smaller).topRight()

    def test_on_a_monitor_with_a_negative_origin(self):
        # Left of and above the primary: no arithmetic may lean on a zero edge.
        bounds = QRectF(-1920, -1080, 1920, 1080)

        placed = QRectF(QRect(FloatingBar.from_spot((1.0, 0.0), bounds, self.SIZE), self.SIZE))

        assert placed.topRight() == self._inside(bounds).topRight()
        assert FloatingBar.spot(placed.topLeft(), bounds, self.SIZE) == (1.0, 0.0)

    def test_a_value_from_outside_the_range_still_lands_inside(self):
        bounds = QRectF(0, 0, 1600, 1000)

        placed = QRectF(QRect(FloatingBar.from_spot((3.0, -2.0), bounds, self.SIZE), self.SIZE))

        assert self._inside(bounds).contains(placed)

    def test_a_bar_wider_than_its_bounds_is_pinned_where_it_always_was(self):
        bounds = QRectF(0, 0, 400, 1000)

        placed = FloatingBar.from_spot((1.0, 0.5), bounds, self.SIZE)

        assert placed.x() == self.MARGIN


class TestTheBarWithNoRoomAnywhere:
    """#50 through the overlay, on a single monitor -- where a selection with
    no room beside it is worst, because there is no other screen at all.
    """

    SIZE = (1600, 1000)
    MARGIN = tokens.BarMetric.BAR_EDGE_MARGIN
    OFFSET = tokens.BarMetric.BAR_OFFSET_Y
    LEFT = Qt.MouseButton.LeftButton
    NONE = Qt.MouseButton.NoButton

    def _overlay(self, monkeypatch, size=SIZE) -> OverlayWindow:
        monkeypatch.setattr(
            overlay_module.platform.current, "reserved_margins", lambda screen: QMargins()
        )
        frame = make_frame(image_size=size, logical_size=size)
        overlay = OverlayWindow(frame, monitor_geometries=[QRectF(0, 0, *size)])
        overlay.show()
        QTest.qWaitForWindowExposed(overlay)
        return overlay

    def _whole(self, overlay: OverlayWindow) -> OverlayWindow:
        monitor = overlay._monitor_geometries[0].toRect()
        overlay.set_selection(QRect(0, 0, monitor.width(), monitor.height()))
        assert overlay._bar.isVisible()
        return overlay

    def _inside(self, overlay: OverlayWindow) -> QRectF:
        bounds = overlay._chrome_bounds()
        return bounds.adjusted(self.MARGIN, self.MARGIN, -self.MARGIN, -self.MARGIN)

    def test_with_nothing_remembered_it_sits_where_it_always_has(self, monkeypatch):
        overlay = self._whole(self._overlay(monkeypatch))

        bar = QRectF(overlay._bar.geometry())

        assert bar.bottom() == overlay._chrome_bounds().bottom() - self.MARGIN
        assert self._inside(overlay).contains(bar)

    def test_a_drag_moves_it_off_what_it_covered(self, monkeypatch):
        overlay = self._whole(self._overlay(monkeypatch))
        before = overlay._bar.pos()

        _drag_bar(overlay._bar, QPointF(-400, -600))

        assert overlay._bar.pos() == before + QPoint(-400, -600)

    def test_where_it_was_dragged_is_remembered(self, monkeypatch):
        overlay = self._whole(self._overlay(monkeypatch))
        assert setup_desktop.load_bar_position() is None

        _drag_bar(overlay._bar, QPointF(-400, -600))

        bar = overlay._bar
        expected = FloatingBar.spot(QPointF(bar.pos()), overlay._chrome_bounds(), bar.size())
        remembered = setup_desktop.load_bar_position()
        assert remembered.monitor == setup_desktop.BAR_ON_OWN_MONITOR
        assert remembered.spot == pytest.approx(expected)

    def test_the_next_snip_with_no_room_puts_it_there(self, monkeypatch):
        first = self._whole(self._overlay(monkeypatch))
        _drag_bar(first._bar, QPointF(-400, -600))
        dragged = first._bar.pos()

        second = self._whole(self._overlay(monkeypatch))

        assert second._bar.pos() == dragged

    def test_it_survives_a_monitor_of_another_size(self, monkeypatch):
        first = self._whole(self._overlay(monkeypatch, size=(2560, 1440)))
        _drag_bar(first._bar, QPointF(-700, -500))
        spot = setup_desktop.load_bar_position().spot

        second = self._whole(self._overlay(monkeypatch, size=(1280, 800)))

        bar = second._bar
        assert bar.pos() == FloatingBar.from_spot(spot, second._chrome_bounds(), bar.size())
        assert self._inside(second).contains(QRectF(bar.geometry()))

    def test_a_corner_it_was_dragged_into_is_that_corner_on_a_smaller_monitor(
        self, monkeypatch
    ):
        first = self._whole(self._overlay(monkeypatch, size=(2560, 1440)))
        _drag_bar(first._bar, QPointF(5000, -5000))

        second = self._whole(self._overlay(monkeypatch, size=(1280, 800)))

        assert QRectF(second._bar.geometry()).topRight() == self._inside(second).topRight()

    def test_a_remembered_place_is_not_used_while_there_is_room(self, monkeypatch):
        setup_desktop.save_bar_position(
            setup_desktop.BarPosition(setup_desktop.BAR_ON_OWN_MONITOR, (0.5, 0.5))
        )
        overlay = self._overlay(monkeypatch)
        selection = QRect(700, 400, 200, 150)
        bar = overlay._bar
        # Where the remembered place would have put it: over the selection.
        remembered = QRect(
            FloatingBar.from_spot((0.5, 0.5), overlay._chrome_bounds(), bar.sizeHint()),
            bar.sizeHint(),
        )
        assert QRectF(remembered).intersects(QRectF(selection))

        overlay.set_selection(selection)

        assert not QRectF(bar.geometry()).intersects(QRectF(selection))
        assert QRectF(bar.geometry()).top() == QRectF(selection).bottom() + self.OFFSET

    def test_a_drag_beside_a_selection_with_room_is_not_remembered(self, monkeypatch):
        overlay = self._overlay(monkeypatch)
        overlay.set_selection(QRect(700, 400, 200, 150))

        _drag_bar(overlay._bar, QPointF(-300, 250))

        assert setup_desktop.load_bar_position() is None
        self._whole(overlay)
        bar = QRectF(overlay._bar.geometry())
        assert bar.bottom() == overlay._chrome_bounds().bottom() - self.MARGIN

    def test_the_dragged_place_holds_while_the_snip_is_marked_up(self, monkeypatch):
        overlay = self._overlay(monkeypatch)
        selection = QRect(700, 400, 200, 150)
        overlay.set_selection(selection)
        _drag_bar(overlay._bar, QPointF(-300, 250))
        dragged = overlay._bar.pos()

        overlay.add_mark(
            Rectangle(
                colour=QColor("#ff0000"),
                stroke_width=4,
                start=QPointF(710, 410),
                end=QPointF(800, 500),
            )
        )
        overlay.set_selection(QRect(selection))

        assert overlay._bar.pos() == dragged

    def test_a_stored_place_it_cannot_read_leaves_placement_automatic(self, monkeypatch):
        path = setup_desktop.config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"bar_position": [0.5, "low"]}')

        overlay = self._whole(self._overlay(monkeypatch))

        bar = QRectF(overlay._bar.geometry())
        assert bar.bottom() == overlay._chrome_bounds().bottom() - self.MARGIN

    # -- a drag ends without its release, as a mark's does ----------------

    def _mid_drag(self, monkeypatch) -> OverlayWindow:
        overlay = self._whole(self._overlay(monkeypatch))
        _drag_bar(overlay._bar, QPointF(-200, -300), release=False)
        assert overlay._bar.is_dragging
        return overlay

    def test_a_move_with_no_button_held_ends_it_where_it_stood(self, monkeypatch):
        overlay = self._mid_drag(monkeypatch)
        dragged = overlay._bar.pos()

        _send_mouse(overlay, "move", QPoint(50, 50), self.NONE)

        assert not overlay._bar.is_dragging
        assert overlay._bar.pos() == dragged
        assert setup_desktop.load_bar_position() is not None

    def test_later_movement_no_longer_moves_it(self, monkeypatch):
        overlay = self._mid_drag(monkeypatch)
        dragged = overlay._bar.pos()
        _send_mouse(overlay, "move", QPoint(50, 50), self.NONE)

        _bar_pointer(overlay._bar, "move", QPointF(400, 400), self.LEFT)

        assert overlay._bar.pos() == dragged

    def test_a_press_on_the_frame_ends_it(self, monkeypatch):
        overlay = self._mid_drag(monkeypatch)
        dragged = overlay._bar.pos()

        _send_mouse(overlay, "press", QPoint(50, 50), self.LEFT)

        assert not overlay._bar.is_dragging
        assert overlay._bar.pos() == dragged

    def test_focus_leaving_the_window_ends_it(self, monkeypatch):
        overlay = self._mid_drag(monkeypatch)

        QApplication.sendEvent(overlay, QEvent(QEvent.Type.WindowDeactivate))

        assert not overlay._bar.is_dragging

    # -- what hangs off the bar -------------------------------------------

    def test_a_drag_closes_an_open_family_menu(self, monkeypatch):
        overlay = self._whole(self._overlay(monkeypatch))
        overlay._toggle_family_menu("shapes")
        menu = overlay._family_menus["shapes"]
        assert not menu.isHidden()

        _drag_bar(overlay._bar, QPointF(0, -400))

        assert menu.isHidden()

    def test_a_drag_closes_an_open_watermark_menu(self, monkeypatch):
        # #69's menu is anchored to its slot, as a family's is, so a bar on
        # the move takes it down the same way.
        overlay = self._whole(self._overlay(monkeypatch))
        overlay._toggle_watermark_menu()
        menu = overlay._watermark_menu
        assert not menu.isHidden()

        _drag_bar(overlay._bar, QPointF(0, -400))

        assert menu.isHidden()

    def test_a_drag_closes_the_style_popover_and_the_tool_hint_follows(self, monkeypatch):
        overlay = self._whole(self._overlay(monkeypatch))
        overlay._bar.select_tool("pen")
        overlay._toggle_style()
        assert overlay._style_popover.isVisible()

        _drag_bar(overlay._bar, QPointF(0, -400))

        assert not overlay._style_popover.isVisible()
        assert not overlay._bar._style_dot.is_open
        hint = overlay._tool_hint
        assert hint.isVisible()
        assert hint.geometry().center().x() == pytest.approx(
            overlay._bar.geometry().center().x(), abs=1
        )
        assert overlay._chrome_bounds().contains(QRectF(hint.geometry()))

    def test_a_press_that_goes_nowhere_leaves_a_menu_open(self, monkeypatch):
        overlay = self._whole(self._overlay(monkeypatch))
        overlay._toggle_family_menu("redact")

        _drag_bar(overlay._bar, QPointF(1, 1))

        assert not overlay._family_menus["redact"].isHidden()

    def test_a_bar_dragged_over_the_selection_is_not_in_the_export(self, monkeypatch):
        overlay = self._whole(self._overlay(monkeypatch, size=(600, 600)))
        _drag_bar(overlay._bar, QPointF(0, -250))
        bar = overlay._bar.geometry()
        assert overlay._bar.isVisible()
        assert QRectF(overlay._selection).contains(QRectF(bar))

        rendered = overlay.rendered_image()

        assert pixel(rendered, bar.center()) == QColor(10, 20, 30)
        assert pixel(rendered, bar.topLeft() + QPoint(4, 4)) == QColor(10, 20, 30)


class TestADraggedBarClearsTheDock:
    """Dragged or remembered, the bar and everything hanging off it stay
    inside `_chrome_bounds` -- the monitor less the top bar and the dock.
    """

    DOCK = QMargins(0, 32, 0, 71)
    MARGIN = tokens.BarMetric.BAR_EDGE_MARGIN

    def _overlay(self, monkeypatch) -> OverlayWindow:
        # As `TestChromeClearsTheDesktopsOwnDock` builds it: the whole
        # offscreen screen, so the platform is actually asked for margins.
        monkeypatch.setattr(
            overlay_module.platform.current, "reserved_margins", lambda screen: self.DOCK
        )
        available = QGuiApplication.primaryScreen().geometry()
        size = (available.width(), available.height())
        frame = make_frame(image_size=size, logical_size=size)
        overlay = OverlayWindow(frame)
        overlay.setGeometry(0, 0, *size)
        overlay.show()
        QTest.qWaitForWindowExposed(overlay)
        overlay.set_selection(overlay.rect())
        assert overlay._bar.isVisible()
        return overlay

    @staticmethod
    def _assert_inside(bounds: QRectF, bar: QRectF) -> None:
        # The offscreen screen is 533 logical px wide at 1.5x, narrower than
        # the bar itself, so no placement can contain it sideways there.
        # What this class is about -- the top bar and the dock -- is
        # vertical, and holds at any scale; the sides are checked wherever
        # the bar can fit at all.
        assert bounds.top() <= bar.top() and bar.bottom() <= bounds.bottom()
        if bar.width() <= bounds.width():
            assert bounds.contains(bar)

    def test_dragging_it_down_stops_above_a_bottom_dock(self, monkeypatch):
        overlay = self._overlay(monkeypatch)

        _drag_bar(overlay._bar, QPointF(0, 5000))

        bar = QRectF(overlay._bar.geometry())
        self._assert_inside(overlay._chrome_bounds(), bar)
        assert bar.bottom() == overlay.rect().height() - self.DOCK.bottom() - self.MARGIN

    def test_dragging_it_up_stops_below_the_top_bar(self, monkeypatch):
        overlay = self._overlay(monkeypatch)

        _drag_bar(overlay._bar, QPointF(0, -5000))

        assert overlay._bar.geometry().top() == self.DOCK.top() + self.MARGIN

    def test_a_remembered_corner_lands_above_the_dock(self, monkeypatch):
        setup_desktop.save_bar_position(
            setup_desktop.BarPosition(setup_desktop.BAR_ON_OWN_MONITOR, (1.0, 1.0))
        )

        overlay = self._overlay(monkeypatch)

        bar = QRectF(overlay._bar.geometry())
        self._assert_inside(overlay._chrome_bounds(), bar)
        assert bar.bottom() == overlay.rect().height() - self.DOCK.bottom() - self.MARGIN

    def test_menus_the_style_popover_and_the_hint_stay_inside_after_a_drag_into_a_corner(
        self, monkeypatch
    ):
        overlay = self._overlay(monkeypatch)
        overlay._bar.select_tool("pen")
        _drag_bar(overlay._bar, QPointF(5000, 5000))
        bounds = overlay._chrome_bounds()

        assert overlay._tool_hint.isVisible()
        assert bounds.contains(QRectF(overlay._tool_hint.geometry()))

        overlay._toggle_family_menu("redact")
        assert bounds.contains(QRectF(overlay._family_menus["redact"].geometry()))

        overlay._toggle_style()
        assert overlay._style_popover.isVisible()
        # 216px wide, so unlike the tray it replaced it fits across even the
        # offscreen screen at 1.5x, 533 logical px.
        assert bounds.contains(QRectF(overlay._style_popover.geometry()))


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

        QTest.mouseClick(bar._tool_buttons["text"], Qt.MouseButton.LeftButton)

        assert bar.active_tool == "text"
        assert not bar._tool_buttons["pen"].is_active
        assert bar._tool_buttons["text"].is_active

    def test_clicking_a_tool_emits_tool_selected(self):
        bar = FloatingBar()
        received = Mock()
        bar.toolSelected.connect(received)

        QTest.mouseClick(bar._tool_buttons["redact"], Qt.MouseButton.LeftButton)

        received.assert_called_once_with("blur")

    def test_a_family_slot_reads_active_for_any_of_its_siblings(self):
        bar = FloatingBar()

        bar.select_tool("arrow")

        assert bar._tool_buttons["shapes"].is_active
        assert bar._tool_buttons["shapes"].notch._lit
        assert not bar._tool_buttons["redact"].notch._lit

    def test_a_family_slot_shows_its_last_used_sibling(self):
        bar = FloatingBar()
        bar.select_tool("ellipse")
        bar.select_tool("pen")

        assert bar._tool_buttons["shapes"]._icon_name == "ellipse"

        QTest.mouseClick(bar._tool_buttons["shapes"], Qt.MouseButton.LeftButton)

        assert bar.active_tool == "ellipse"

    def test_a_second_click_asks_for_the_menu_and_never_cycles(self):
        bar = FloatingBar()
        asked = Mock()
        bar.familyMenuRequested.connect(asked)
        slot = bar._tool_buttons["redact"]

        QTest.mouseClick(slot, Qt.MouseButton.LeftButton)
        asked.assert_not_called()
        QTest.mouseClick(slot, Qt.MouseButton.LeftButton)

        asked.assert_called_once_with("redact")
        assert bar.active_tool == "blur"

    def test_a_second_click_picks_nothing(self):
        # A picked tool closes whatever menu is open, so a second click that
        # also reported a pick would close the menu it had just asked for.
        bar = FloatingBar()
        slot = bar._tool_buttons["shapes"]
        QTest.mouseClick(slot, Qt.MouseButton.LeftButton)
        picked, selected = Mock(), Mock()
        bar.toolPicked.connect(picked)
        bar.toolSelected.connect(selected)

        QTest.mouseClick(slot, Qt.MouseButton.LeftButton)

        picked.assert_not_called()
        selected.assert_not_called()

    def test_a_family_slot_arms_its_sibling_while_another_familys_is_armed(self):
        bar = FloatingBar()
        asked = Mock()
        bar.familyMenuRequested.connect(asked)
        bar.select_tool("pixelate")

        QTest.mouseClick(bar._tool_buttons["shapes"], Qt.MouseButton.LeftButton)

        assert bar.active_tool == "rect"
        asked.assert_not_called()

    def test_a_plain_slot_clicked_again_stays_armed_and_asks_for_nothing(self):
        bar = FloatingBar()
        asked = Mock()
        bar.familyMenuRequested.connect(asked)

        QTest.mouseClick(bar._tool_buttons["pen"], Qt.MouseButton.LeftButton)
        QTest.mouseClick(bar._tool_buttons["pen"], Qt.MouseButton.LeftButton)

        assert bar.active_tool == "pen"
        asked.assert_not_called()

    def test_pixelate_is_drawn_with_the_mask_glyph(self):
        bar = FloatingBar()

        bar.select_tool("pixelate")

        assert bar._tool_buttons["redact"]._icon_name == "mask"

    def test_a_notch_asks_for_its_familys_menu_and_arms_nothing(self):
        bar = FloatingBar()
        asked = Mock()
        bar.familyMenuRequested.connect(asked)

        QTest.mouseClick(bar._tool_buttons["shapes"].notch, Qt.MouseButton.LeftButton)

        asked.assert_called_once_with("shapes")
        assert bar.active_tool is None

    @pytest.mark.parametrize("slot", ["shapes", "redact", "watermark"])
    def test_a_notch_answers_across_its_slots_corner(self, slot):
        # #77: the 9px box the spec draws the triangle in was missed often
        # enough that the menu seemed not to open at all.
        bar = FloatingBar()
        button = bar._watermark if slot == "watermark" else bar._tool_buttons[slot]
        side = tokens.BarMetric.BTN

        assert button.childAt(QPoint(side - 12, side - 12)) is button.notch
        assert button.childAt(QPoint(side - 1, side - 1)) is button.notch
        # The middle of the glyph, and either edge beside the corner, stay
        # the slot's.
        assert button.childAt(QPoint(side // 2, side // 2)) is None
        assert button.childAt(QPoint(side - 13, side - 1)) is None
        assert button.childAt(QPoint(side - 1, side - 13)) is None

    def test_the_notchs_triangle_stays_where_the_spec_draws_it(self):
        # Flush right in a 9px box 1px in from the slot's corner, however
        # big the area that answers a press.
        slot = FloatingBar()._tool_buttons["shapes"]
        # The notch alone, onto a clear image the slot's size and in the
        # slot's coordinates: grab() paints a background in under it.
        image = QImage(slot.size(), QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(Qt.GlobalColor.transparent)
        slot.notch.render(image, slot.notch.pos(), QRegion(), QWidget.RenderFlag.DrawChildren)
        side = tokens.BarMetric.BTN

        def painted(x, y):
            return image.pixelColor(x, y).alpha() > 0

        # Its right angle sits at (side - 1, side - 3), its legs 5px long.
        assert painted(side - 2, side - 4)
        assert painted(side - 3, side - 5)
        assert not painted(side - 6, side - 8)
        assert not painted(side - 12, side - 12)


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
            tokens.BarMetric.ICON, tokens.BarMetric.ICON, QIcon.Mode.Disabled
        )
        image = pixmap.toImage()
        strongest = max(
            (image.pixelColor(x, y) for x in range(image.width()) for y in range(image.height())),
            key=lambda c: c.alpha(),
        )

        expected = QColor(tokens.BarColor.TOOL_DISABLED_FG)
        assert strongest.red() == pytest.approx(expected.red(), abs=2)
        assert strongest.green() == pytest.approx(expected.green(), abs=2)
        assert strongest.blue() == pytest.approx(expected.blue(), abs=2)


class TestFloatingBarTooltips:
    """SNX-40: 'each button has a tooltip naming the control and its
    shortcut.'
    """

    @pytest.mark.parametrize(
        "slot,tooltip",
        [
            ("pen", "Pen — P"),
            ("highlighter", "Highlighter — H"),
            ("shapes", "Rectangle — R · click again for more shapes"),
            ("step", "Numbered step — S"),
            ("text", "Text — T"),
            ("redact", "Blur — B · click again to switch"),
            ("eraser", "Eraser — E"),
        ],
    )
    def test_each_slot_names_its_tool_and_the_key_that_reaches_it(self, slot, tooltip):
        bar = FloatingBar()

        assert bar._tool_buttons[slot].toolTip() == tooltip

    def test_a_family_slots_tooltip_follows_its_sibling(self):
        bar = FloatingBar()

        bar.select_tool("arrow")
        bar.select_tool("blackout")

        assert bar._tool_buttons["shapes"].toolTip() == "Arrow — A · click again for more shapes"
        assert bar._tool_buttons["redact"].toolTip() == "Blackout — B · click again to switch"

    def test_each_notch_says_what_it_opens(self):
        bar = FloatingBar()

        assert bar._tool_buttons["shapes"].notch.toolTip() == "Choose a shape"
        assert bar._tool_buttons["redact"].notch.toolTip() == "Redaction mode"

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
        expected_top = QRectF(selection).bottom() + tokens.BarMetric.BAR_OFFSET_Y
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
            output_module, "copy_image_to_clipboard", lambda image: calls.append(image)
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
        monkeypatch.setattr(output_module, "copy_image_to_clipboard", lambda image: None)
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


def _send_mouse(widget, kind: str, pos: QPoint, buttons) -> None:
    """A left press, or a move, sent straight to `widget` with exactly the
    buttons asked for -- which QTest cannot do for a move with nothing held
    after a press it never released.
    """
    from PyQt6.QtCore import QEvent
    from PyQt6.QtGui import QMouseEvent

    event_type, button = {
        "press": (QEvent.Type.MouseButtonPress, Qt.MouseButton.LeftButton),
        "move": (QEvent.Type.MouseMove, Qt.MouseButton.NoButton),
    }[kind]
    event = QMouseEvent(
        event_type,
        QPointF(pos),
        QPointF(widget.mapToGlobal(pos)),
        button,
        buttons,
        Qt.KeyboardModifier.NoModifier,
    )
    QApplication.sendEvent(widget, event)


class TestADragEndsWithoutItsRelease:
    """A lost release never leaves a mark stretching. A drag ends on its
    release, on a move with no button held, on a new press and on focus
    leaving the window -- and it ends as it stood at its last move, not as
    it was when its press began.
    """

    LEFT = Qt.MouseButton.LeftButton
    NONE = Qt.MouseButton.NoButton

    def _overlay(self, tool: str | None = "rect") -> OverlayWindow:
        frame = make_frame(image_size=(400, 400), logical_size=(400, 400))
        overlay = OverlayWindow(frame)
        overlay.set_selection(QRect(0, 0, 400, 400))
        if tool is not None:
            overlay._bar.select_tool(tool)
        return overlay

    def _dragged(self) -> OverlayWindow:
        overlay = self._overlay()
        _send_mouse(overlay, "press", QPoint(50, 50), self.LEFT)
        _send_mouse(overlay, "move", QPoint(150, 120), self.LEFT)
        assert overlay._in_progress_shape is not None
        return overlay

    def test_a_move_with_no_button_held_commits_the_mark(self):
        overlay = self._dragged()

        _send_mouse(overlay, "move", QPoint(300, 300), self.NONE)

        assert overlay._in_progress_shape is None
        assert len(overlay.marks) == 1
        # As the drag stood at its last move, not where the pointer went on to.
        assert overlay.marks[0].end == QPointF(150, 120)

    def test_later_movement_no_longer_stretches_it(self):
        overlay = self._dragged()
        _send_mouse(overlay, "move", QPoint(300, 300), self.NONE)

        _send_mouse(overlay, "move", QPoint(380, 380), self.LEFT)

        assert overlay.marks[0].end == QPointF(150, 120)

    def test_a_new_press_ends_the_open_drag_before_starting_its_own(self):
        overlay = self._dragged()

        _send_mouse(overlay, "press", QPoint(250, 250), self.LEFT)

        assert len(overlay.marks) == 1
        assert overlay.marks[0].end == QPointF(150, 120)
        assert overlay._in_progress_shape.start == QPointF(250, 250)

    def test_focus_leaving_the_window_commits_it(self):
        from PyQt6.QtCore import QEvent

        overlay = self._dragged()

        QApplication.sendEvent(overlay, QEvent(QEvent.Type.WindowDeactivate))

        assert overlay._in_progress_shape is None
        assert len(overlay.marks) == 1

    def test_a_lost_release_stops_an_eraser_sweep(self):
        overlay = self._overlay(tool="eraser")
        _send_mouse(overlay, "press", QPoint(200, 200), self.LEFT)
        assert overlay._erasing

        _send_mouse(overlay, "move", QPoint(210, 210), self.NONE)

        assert not overlay._erasing

    def test_a_lost_release_stops_a_reframe(self):
        overlay = self._overlay(tool=None)
        overlay.set_selection(QRect(100, 100, 200, 200))
        _send_mouse(overlay, "press", QPoint(300, 300), self.LEFT)
        assert overlay._active_handle is not None
        _send_mouse(overlay, "move", QPoint(320, 320), self.LEFT)
        reframed = QRect(overlay._selection)

        _send_mouse(overlay, "move", QPoint(380, 380), self.NONE)

        assert overlay._active_handle is None
        assert overlay._selection == reframed

    def test_a_lost_release_keeps_the_region_its_last_move_drew(self):
        frame = make_frame(image_size=(400, 400), logical_size=(400, 400))
        overlay = OverlayWindow(frame)
        _send_mouse(overlay, "press", QPoint(100, 100), self.LEFT)
        _send_mouse(overlay, "move", QPoint(300, 250), self.LEFT)

        _send_mouse(overlay, "move", QPoint(390, 390), self.NONE)

        assert overlay._region_drag_anchor is None
        assert overlay._selection == QRect(100, 100, 200, 150)


class TestBlackoutBakesItsFill:
    def test_the_exported_pixels_are_the_blackout_fill(self):
        frame = make_gradient_frame()
        overlay = OverlayWindow(frame)
        overlay.set_selection(QRect(0, 0, 200, 200))
        overlay._bar.select_tool("blackout")

        QTest.mousePress(overlay, Qt.MouseButton.LeftButton, pos=QPoint(40, 40))
        QTest.mouseMove(overlay, QPoint(140, 120))
        QTest.mouseRelease(overlay, Qt.MouseButton.LeftButton, pos=QPoint(140, 120))

        rendered = overlay.rendered_image()

        fill = QColor(tokens.BLACKOUT_FILL)
        for point in (QPoint(41, 41), QPoint(90, 80), QPoint(139, 119)):
            assert pixel(rendered, point) == fill, point
        assert pixel(rendered, QPoint(150, 130)) != fill


def _styled_overlay(selection=QRect(400, 200, 400, 300)) -> OverlayWindow:
    frame = make_frame(image_size=(1600, 1000), logical_size=(1600, 1000))
    overlay = OverlayWindow(frame)
    overlay.show()
    QTest.qWaitForWindowExposed(overlay)
    overlay.set_selection(selection)
    return overlay


def _close_to(colour: QColor, hex_colour: str, tolerance: int = 10) -> bool:
    """Within `tolerance` of `hex_colour` on every channel -- for a pixel an
    antialiased edge may have nudged.
    """
    target = QColor(hex_colour)
    return all(
        abs(a - b) <= tolerance
        for a, b in zip(colour.getRgb()[:3], target.getRgb()[:3])
    )


class TestStyleDot:
    """#68: the style dot is its own preview -- a dot in the active tool's
    colour at its stroke's diameter, a ring for a shape that is outline
    only, dimmed for a tool with nothing to style -- and it opens the style
    popover.
    """

    RED = "#ef4444"

    @staticmethod
    def _dot_pixel(bar: FloatingBar, dx: float = 0.0) -> QColor:
        """A pixel `dx` right of the dot's centre, as the bar draws it.
        Grabbed through the bar, not the dot alone: a grab of the bare dot
        has no alpha channel, so a dimmed dot would read as fully opaque.
        """
        bar.resize(bar.sizeHint())
        image = bar.grab().toImage()
        centre = QRectF(bar._style_dot.geometry()).center()
        return pixel(image, centre.x() + dx, centre.y())

    def test_it_is_a_dot_in_the_tools_colour_at_its_stroke(self):
        bar = FloatingBar()
        bar.select_tool("pen")
        bar.set_style_preview(ToolStyle(colour=self.RED, size=10))

        centre = self._dot_pixel(bar)
        inside = self._dot_pixel(bar, 6)
        outside = self._dot_pixel(bar, 11)

        assert (centre.red(), centre.green(), centre.blue(), centre.alpha()) == (
            0xEF, 0x44, 0x44, 255
        )
        # 10px at the spec's scale is a 16px dot: 6px out is still the ink,
        # 11px out is past it and its ring.
        assert bar._style_dot.diameter == 10 * tokens.BarMetric.STYLE_DOT_SCALE
        assert _close_to(inside, self.RED)
        assert not _close_to(outside, self.RED, tolerance=60)

    def test_a_shape_that_is_outline_only_is_a_ring(self):
        bar = FloatingBar()
        bar.select_tool("rect")
        bar.set_style_preview(ToolStyle(colour=self.RED, size=10, fill="outline"))

        # A 16px dot with a 2px ring inside its edge: 6 to 8px out is the
        # ring, the centre is the button showing through. The ring is looked
        # for across its width rather than at one pixel: at a fractional scale
        # the dot's centre can fall between physical pixels, and which side it
        # rounds to follows the bar's width -- so the UI font -- which put a
        # single sample on the ring's antialiased edge in DejaVu Sans at 1.5x.
        red = QColor(self.RED)
        ring = min(
            (self._dot_pixel(bar, dx) for dx in (6.5, 7.0, 7.5)),
            key=lambda c: sum(abs(a - b) for a, b in zip(c.getRgb()[:3], red.getRgb()[:3])),
        )
        hole = self._dot_pixel(bar)

        assert bar._style_dot.is_ring
        assert _close_to(ring, self.RED)
        assert not _close_to(hole, self.RED, tolerance=60)

    @pytest.mark.parametrize("fill", ["filled", "both"])
    def test_a_shape_with_any_fill_is_a_dot(self, fill):
        bar = FloatingBar()
        bar.select_tool("ellipse")
        bar.set_style_preview(ToolStyle(colour=self.RED, size=10, fill=fill))

        assert not bar._style_dot.is_ring
        assert _close_to(self._dot_pixel(bar), self.RED)

    def test_a_tool_with_no_fill_is_never_a_ring(self):
        # A line remembers "outline" like every tool, but a ring would say
        # something about it that is not true.
        bar = FloatingBar()
        bar.select_tool("line")
        bar.set_style_preview(ToolStyle(colour=self.RED, size=10, fill="outline"))

        assert not bar._style_dot.is_ring
        assert _close_to(self._dot_pixel(bar), self.RED)

    def test_it_dims_for_a_tool_with_nothing_to_style(self):
        bar = FloatingBar()
        bar.set_style_preview(ToolStyle(colour=self.RED, size=10))
        bar.select_tool("pen")
        lit = self._dot_pixel(bar)

        bar.select_tool("eraser")
        dimmed = self._dot_pixel(bar)

        # At a third of its opacity the dot mostly shows the bar's dark glass
        # through it, so its red falls well short of the ink's.
        assert dimmed.red() < lit.red() - 60

    def test_its_tooltip_reads_the_colour_and_stroke(self):
        bar = FloatingBar()
        bar.select_tool("pen")
        bar.set_style_preview(ToolStyle(colour=self.RED, size=7))

        assert bar._style_dot.toolTip() == f"Pen · {self.RED} · 7px — click for style"

    def test_a_redactions_tooltip_reads_its_strength(self):
        bar = FloatingBar()
        bar.select_tool("pixelate")
        bar.set_style_preview(ToolStyle(colour=self.RED, size=7, strength=12))

        assert bar._style_dot.toolTip() == "Pixelate · strength 12 — click for style"

    def test_it_will_not_open_for_a_tool_with_nothing_to_style(self):
        overlay = _styled_overlay()
        QTest.mouseClick(overlay._bar._tool_buttons["eraser"], Qt.MouseButton.LeftButton)
        dot = overlay._bar._style_dot

        QTest.mouseClick(dot, Qt.MouseButton.LeftButton)

        assert overlay._style_popover.isHidden()
        assert not dot.is_open
        assert dot.toolTip() == "Eraser has nothing to style"

    def test_blackout_has_nothing_to_style_either(self):
        bar = FloatingBar()

        bar.select_tool("blackout")

        assert not bar._style_dot.is_stylable
        assert bar._style_dot.toolTip() == "Blackout has nothing to style"

    def test_it_opens_the_popover_and_closes_it_again(self):
        overlay = _styled_overlay()
        QTest.mouseClick(overlay._bar._tool_buttons["redact"], Qt.MouseButton.LeftButton)
        dot = overlay._bar._style_dot

        QTest.mouseClick(dot, Qt.MouseButton.LeftButton)
        assert overlay._style_popover.isVisible()
        assert dot.is_open

        QTest.mouseClick(dot, Qt.MouseButton.LeftButton)
        assert not overlay._style_popover.isVisible()
        assert not dot.is_open
        assert overlay._tool_hint.isVisible()

    def test_the_active_tools_own_style_is_what_it_shows(self):
        overlay = _styled_overlay()

        overlay._bar.select_tool("rect")

        assert overlay._bar._style_dot.style == overlay._styles.of("rect")

    def test_opening_a_family_menu_closes_the_popover(self):
        # One menu at a time, and the popover counts as one.
        overlay = _styled_overlay()
        QTest.mouseClick(overlay._bar._tool_buttons["pen"], Qt.MouseButton.LeftButton)
        QTest.mouseClick(overlay._bar._style_dot, Qt.MouseButton.LeftButton)

        QTest.mouseClick(overlay._bar._tool_buttons["shapes"].notch, Qt.MouseButton.LeftButton)

        assert overlay._family_menus["shapes"].isVisible()
        assert not overlay._style_popover.isVisible()


class TestStylePopoverSections:
    """#68: 'The popover shows only what the active tool supports
    (STYLE_SECTIONS) ... A section the tool cannot use is not rendered --
    never rendered and inert.'"""

    @pytest.mark.parametrize(
        "tool,sections",
        [(tool, sections) for tool, sections in tokens.STYLE_SECTIONS.items() if sections],
    )
    def test_each_tool_shows_its_own_sections_and_no_others(self, tool, sections):
        popover = StylePopover(ToolStyles())

        popover.set_tool(tool)

        assert popover.sections() == sections

    def test_a_section_left_out_leaves_no_gap_where_it_would_be(self):
        popover = StylePopover(ToolStyles())
        popover.set_tool("pen")
        popover.grab()

        assert popover._fill_button.isHidden() and popover._dash_button.isHidden()
        assert popover._size_slider.geometry().left() == 0

    def test_a_redaction_is_one_row(self):
        styles = ToolStyles()
        blur = StylePopover(styles)
        blur.set_tool("blur")
        rect = StylePopover(styles)
        rect.set_tool("rect")
        metric = tokens.BarMetric

        assert blur._colour_row.isHidden()
        assert rect.height() == (
            2 * (metric.BORDER + metric.STYLE_PAD_V)
            + metric.SWATCH_H
            + metric.STYLE_ROW_GAP
            + metric.CYCLE_H
        )
        assert blur.height() < rect.height() - metric.SWATCH_H

    @pytest.mark.parametrize("before,after", [("pen", "rect"), ("rect", "pen"), ("blur", "arrow")])
    def test_switching_tools_sizes_the_popover_for_the_new_tool_at_once(self, before, after):
        # A popover already laid out for one tool, then handed another, kept
        # the first tool's height until Qt's layout event arrived -- so the
        # first open after switching from the pen to a rectangle cut the fill
        # and line buttons off at the bottom.
        styles = ToolStyles()
        popover = StylePopover(styles)
        popover.set_tool(before)
        popover.grab()
        QApplication.processEvents()

        popover.set_tool(after)

        settled = StylePopover(styles)
        settled.set_tool(after)
        settled.grab()
        QApplication.processEvents()
        settled.adjustSize()
        assert popover.height() == settled.height()
        if after == "rect":
            metric = tokens.BarMetric
            assert popover.height() == (
                2 * (metric.BORDER + metric.STYLE_PAD_V)
                + metric.SWATCH_H
                + metric.STYLE_ROW_GAP
                + metric.CYCLE_H
            )

    def test_switching_tools_swaps_the_sections(self):
        popover = StylePopover(ToolStyles())
        popover.set_tool("blur")

        popover.set_tool("arrow")

        assert popover.sections() == ["color", "dash", "size"]

    def test_the_size_slider_is_named_for_what_it_sizes(self):
        popover = StylePopover(ToolStyles())

        popover.set_tool("text")
        text = popover._size_slider.toolTip()
        popover.set_tool("pen")

        assert text == "Text size — [ ]"
        assert popover._size_slider.toolTip() == "Stroke — [ ]"


class TestStylePopoverComposition:
    """#68: 216px wide; seven swatches and `+` across one row; a slider with
    a mono readout -- and nothing in it that takes the keyboard.
    """

    @staticmethod
    def _laid_out(tool="rect") -> StylePopover:
        popover = StylePopover(ToolStyles())
        popover.set_tool(tool)
        popover.grab()
        return popover

    def test_it_is_the_handoffs_width(self):
        popover = self._laid_out()

        assert popover.width() == tokens.BarMetric.MENU_W_STYLE
        assert popover.grab().deviceIndependentSize().width() == tokens.BarMetric.MENU_W_STYLE

    def test_the_colour_row_is_the_seven_swatches_then_plus(self):
        popover = self._laid_out()
        row = [*popover._swatch_buttons.values(), popover._custom_button]

        lefts = [button.geometry().left() for button in row]

        assert list(popover._swatch_buttons) == [hex for _name, hex in tokens.INK_SWATCHES]
        assert lefts == sorted(set(lefts))

    def test_the_eight_share_the_row_and_fit_inside_it(self):
        popover = self._laid_out()
        row = [*popover._swatch_buttons.values(), popover._custom_button]
        widths = [button.width() for button in row]

        assert max(widths) - min(widths) <= 1
        assert row[-1].geometry().right() < popover._colour_row.width()
        inset = tokens.BarMetric.STYLE_PAD_H + tokens.BarMetric.BORDER
        assert popover._colour_row.width() == tokens.BarMetric.MENU_W_STYLE - 2 * inset

    def test_each_swatch_names_its_key(self):
        popover = self._laid_out()

        tooltips = [button.toolTip() for button in popover._swatch_buttons.values()]

        assert tooltips == [
            f"{name} — {index}" for index, (name, _hex) in enumerate(tokens.INK_SWATCHES, 1)
        ]

    def test_the_sliders_cover_the_handoffs_ranges(self):
        popover = self._laid_out()

        assert (popover._size_slider.minimum(), popover._size_slider.maximum()) == (
            tokens.STROKE_RANGE
        )
        assert (popover._strength_slider.minimum(), popover._strength_slider.maximum()) == (
            tokens.STRENGTH_RANGE
        )

    def test_the_readouts_hold_their_width_and_read_the_style(self):
        styles = ToolStyles()
        popover = StylePopover(styles)
        popover.set_tool("pen")
        size_readout = popover._size_readout.text()
        popover.set_tool("blur")

        assert popover._size_readout.minimumWidth() == tokens.BarMetric.READOUT_W_SIZE
        assert popover._strength_readout.minimumWidth() == tokens.BarMetric.READOUT_W_STRENGTH
        assert size_readout == f"{styles.of('pen').size}px"
        assert popover._strength_readout.text() == str(styles.of("blur").strength)

    def test_nothing_in_it_takes_the_keyboard(self):
        # A focused slider or button would keep 1-7, [ ] and D from the
        # window -- the keys the one-row layout is only worth having with.
        popover = self._laid_out()

        focusable = [
            type(child).__name__
            for child in popover.findChildren(QWidget)
            if child.focusPolicy() != Qt.FocusPolicy.NoFocus
        ]

        assert focusable == []


class TestStylePopoverFill:
    """Glass like the family menus: a translucent paint, not a translucent
    widget, so every control on it stays opaque.
    """

    def test_background_pixel_is_painted_at_the_menu_alpha(self):
        popover = StylePopover(ToolStyles())
        popover.set_tool("pen")

        rendered = popover.grab().toImage()
        # Top padding, mid-width: inside the rounded fill, above every
        # control.
        sampled = pixel(rendered, popover.width() // 2, 3)

        assert sampled.alpha() == pytest.approx(round(tokens.BarColor.MENU_BG_ALPHA * 255), abs=2)
        assert _close_to(sampled, tokens.BarColor.MENU_BG, tolerance=2)

    def test_control_pixels_stay_fully_opaque_over_it(self):
        popover = StylePopover(ToolStyles())
        popover.set_tool("pen")

        rendered = popover.grab().toImage()
        button = popover._swatch_buttons[tokens.INK_SWATCHES[1][1]]
        rect = QRect(button.mapTo(popover, QPoint(0, 0)), button.size())
        alphas = [
            pixel(rendered, x, y).alpha()
            for x in range(rect.left(), rect.right())
            for y in range(rect.top(), rect.bottom())
        ]

        assert max(alphas) == 255


class TestStylePopoverSwatches:
    """The picked colour is ringed, and picking one changes the colour the
    tool's next mark is drawn in -- that tool's, and no other's.
    """

    def test_the_tools_own_colour_is_ringed(self):
        styles = ToolStyles()
        popover = StylePopover(styles)

        popover.set_tool("rect")

        ringed = [hex for hex, button in popover._swatch_buttons.items() if button.is_selected]
        assert ringed == [styles.of("rect").colour]

    def test_a_colour_no_swatch_holds_rings_none(self):
        # The highlighter's seed amber is not one of the seven.
        popover = StylePopover(ToolStyles())

        popover.set_tool("highlighter")

        assert not any(button.is_selected for button in popover._swatch_buttons.values())

    def test_clicking_a_swatch_sets_the_tools_colour_and_moves_the_ring(self):
        styles = ToolStyles()
        popover = StylePopover(styles)
        popover.set_tool("pen")
        received = Mock()
        popover.styleChanged.connect(received)
        _name, target = tokens.INK_SWATCHES[4]

        QTest.mouseClick(popover._swatch_buttons[target], Qt.MouseButton.LeftButton)

        assert styles.of("pen").colour == target
        assert styles.of("rect") == ToolStyles().of("rect")
        assert [h for h, b in popover._swatch_buttons.items() if b.is_selected] == [target]
        received.assert_called_once_with("pen")

    def test_an_unpicked_swatch_paints_its_own_colour(self):
        _name, hex_colour = tokens.INK_SWATCHES[3]
        button = _SwatchButton("Emerald", hex_colour, "4")
        button.resize(tokens.BarMetric.SWATCH_H, tokens.BarMetric.SWATCH_H)

        rendered = button.grab().toImage()
        centre = pixel(rendered, button.width() / 2, button.height() / 2)

        assert _close_to(centre, hex_colour, tolerance=0)

    def test_the_picked_swatch_paints_the_double_ring(self):
        _name, hex_colour = tokens.INK_SWATCHES[1]
        button = _SwatchButton("Red", hex_colour, "2")
        button.set_selected(True)
        button.resize(tokens.BarMetric.SWATCH_H, tokens.BarMetric.SWATCH_H)

        rendered = button.grab().toImage()
        centre = pixel(rendered, button.width() / 2, button.height() / 2)
        # The outermost pixel, halfway down its flat side, is the light ring.
        edge = pixel(rendered, 0, button.height() / 2)

        assert _close_to(centre, hex_colour, tolerance=0)
        assert _close_to(edge, tokens.BarColor.SWATCH_RING, tolerance=12)


class TestStylePopoverCustomColour:
    """#68: 'A custom colour works as today's tray's does' -- `+` opens the
    colour dialog on the current colour, and what it returns is the tool's
    colour until something else is picked.
    """

    def _popover(self):
        styles = ToolStyles()
        popover = StylePopover(styles)
        popover.set_tool("arrow")
        return popover, styles

    def test_it_opens_the_dialog_on_the_tools_colour(self, monkeypatch):
        popover, styles = self._popover()
        seen = []
        monkeypatch.setattr(
            QColorDialog,
            "getColor",
            staticmethod(lambda initial, *a, **k: seen.append(initial) or QColor()),
        )

        QTest.mouseClick(popover._custom_button, Qt.MouseButton.LeftButton)

        assert seen == [QColor(styles.of("arrow").colour)]

    def test_a_colour_chosen_becomes_the_tools_colour(self, monkeypatch):
        popover, styles = self._popover()
        chosen = QColor("#336699")
        monkeypatch.setattr(QColorDialog, "getColor", staticmethod(lambda *a, **k: chosen))

        QTest.mouseClick(popover._custom_button, Qt.MouseButton.LeftButton)

        assert styles.of("arrow").colour == chosen.name()
        assert not any(button.is_selected for button in popover._swatch_buttons.values())

    def test_cancelling_the_dialog_changes_nothing(self, monkeypatch):
        popover, styles = self._popover()
        before = styles.of("arrow")
        # QColorDialog.getColor() returns an invalid QColor on Cancel.
        monkeypatch.setattr(QColorDialog, "getColor", staticmethod(lambda *a, **k: QColor()))

        QTest.mouseClick(popover._custom_button, Qt.MouseButton.LeftButton)

        assert styles.of("arrow") == before


class TestFillAndLineClickThrough:
    """#68: 'Fill and line are click-through: a click advances to the next
    state and the button shows the current one.'"""

    def test_a_fill_click_advances_through_the_cycle_and_wraps(self):
        styles = ToolStyles()
        popover = StylePopover(styles)
        popover.set_tool("ellipse")

        seen = [popover._fill_button.state]
        for _ in range(3):
            QTest.mouseClick(popover._fill_button, Qt.MouseButton.LeftButton)
            seen.append(popover._fill_button.state)

        assert seen == ["outline", "filled", "both", "outline"]
        assert styles.of("ellipse").fill == "outline"

    def test_a_line_click_advances_through_the_cycle_and_wraps(self):
        styles = ToolStyles()
        popover = StylePopover(styles)
        popover.set_tool("arrow")

        seen = [popover._dash_button.state]
        for _ in range(3):
            QTest.mouseClick(popover._dash_button, Qt.MouseButton.LeftButton)
            seen.append(styles.of("arrow").dash)

        assert seen == ["solid", "dashed", "dotted", "solid"]

    def test_the_tooltips_name_the_state_and_what_a_click_does(self):
        popover = StylePopover(ToolStyles())
        popover.set_tool("rect")  # seeded dashed, outline and filled

        assert popover._fill_button.toolTip() == "Fill · Outline and filled → click for Outline only"
        assert popover._dash_button.toolTip() == "Line · Dashed → click for Dotted — D"

    def test_the_button_shows_its_state(self):
        filled = _CycleButton("fill")
        filled.set_state("filled", "")
        outline = _CycleButton("fill")
        outline.set_state("outline", "")
        solid = _CycleButton("dash")
        solid.set_state("solid", "")
        dotted = _CycleButton("dash")
        dotted.set_state("dotted", "")

        filled_centre = pixel(filled.grab().toImage(), filled.width() / 2, filled.height() / 2)
        outline_centre = pixel(outline.grab().toImage(), outline.width() / 2, outline.height() / 2)

        assert _close_to(filled_centre, tokens.BarColor.CYCLE_GLYPH, tolerance=4)
        assert not _close_to(outline_centre, tokens.BarColor.CYCLE_GLYPH, tolerance=40)
        assert solid.grab().toImage() != dotted.grab().toImage()


class TestStyleShortcuts:
    """#68: '1-7 pick a colour, [ and ] step the stroke, D cycles line
    style' -- each on the active tool, and only on a tool with that section.
    """

    def _overlay(self, tool="pen"):
        frame = make_frame(image_size=(200, 200), logical_size=(200, 200))
        overlay = OverlayWindow(frame)
        overlay.set_selection(QRect(0, 0, 200, 200))
        overlay._bar.select_tool(tool)
        return overlay

    @pytest.mark.parametrize("number", range(1, 8))
    def test_a_number_picks_its_swatch(self, number):
        overlay = self._overlay()
        _name, hex_colour = tokens.INK_SWATCHES[number - 1]

        QTest.keyClick(overlay, getattr(Qt.Key, f"Key_{number}"))

        assert overlay._styles.of("pen").colour == hex_colour
        assert overlay._bar._style_dot.style.colour == hex_colour

    def test_right_bracket_thickens_and_left_bracket_thins(self):
        overlay = self._overlay()
        start = overlay._styles.of("pen").size

        QTest.keyClick(overlay, Qt.Key.Key_BracketRight)
        thicker = overlay._styles.of("pen").size
        QTest.keyClick(overlay, Qt.Key.Key_BracketLeft)
        QTest.keyClick(overlay, Qt.Key.Key_BracketLeft)

        assert thicker == start + 1
        assert overlay._styles.of("pen").size == start - 1
        assert overlay._bar._style_dot.style.size == start - 1

    def test_the_brackets_stop_at_the_ends_of_the_range(self):
        overlay = self._overlay()
        low, high = tokens.STROKE_RANGE
        overlay._styles.update("pen", size=low)

        QTest.keyClick(overlay, Qt.Key.Key_BracketLeft)
        at_low = overlay._styles.of("pen").size
        overlay._styles.update("pen", size=high)
        QTest.keyClick(overlay, Qt.Key.Key_BracketRight)

        assert (at_low, overlay._styles.of("pen").size) == (low, high)

    def test_the_brackets_step_a_redactions_strength(self):
        overlay = self._overlay("pixelate")
        start = overlay._styles.of("pixelate").strength

        QTest.keyClick(overlay, Qt.Key.Key_BracketRight)

        assert overlay._styles.of("pixelate").strength == start + 1

    def test_d_cycles_the_line_style(self):
        overlay = self._overlay("arrow")

        seen = []
        for _ in range(3):
            QTest.keyClick(overlay, Qt.Key.Key_D)
            seen.append(overlay._styles.of("arrow").dash)

        assert seen == ["dashed", "dotted", "solid"]

    def test_a_key_for_a_section_the_tool_lacks_does_nothing(self):
        pen = self._overlay("pen")
        blur = self._overlay("blur")
        eraser = self._overlay("eraser")

        QTest.keyClick(pen, Qt.Key.Key_D)
        QTest.keyClick(blur, Qt.Key.Key_2)
        QTest.keyClick(eraser, Qt.Key.Key_BracketRight)

        assert pen._styles.of("pen") == ToolStyles().of("pen")
        assert blur._styles.of("blur") == ToolStyles().of("blur")
        assert eraser._styles.of("eraser") == ToolStyles().of("eraser")

    def test_a_key_styles_only_the_active_tool(self):
        overlay = self._overlay("pen")

        QTest.keyClick(overlay, Qt.Key.Key_2)
        QTest.keyClick(overlay, Qt.Key.Key_R)

        assert overlay._styles.of("rect") == ToolStyles().of("rect")
        assert overlay._bar._style_dot.style == overlay._styles.of("rect")

    def test_the_keys_yield_to_a_label_being_typed(self):
        overlay = self._overlay("pen")
        label = QLineEdit(overlay)
        label.setFocus()

        QTest.keyClick(overlay, Qt.Key.Key_2)

        assert overlay._styles.of("pen") == ToolStyles().of("pen")


class TestStylePopoverOverlayIntegration:
    """The popover wired into `OverlayWindow`: where it opens, what keeps it
    open, and that what it sets is what the next mark is drawn with.
    """

    def test_it_opens_above_the_bar_centred_on_the_dot(self):
        overlay = _styled_overlay()
        overlay._bar.select_tool("rect")

        QTest.mouseClick(overlay._bar._style_dot, Qt.MouseButton.LeftButton)

        popover = overlay._style_popover.geometry()
        bar = overlay._bar.geometry()
        dot = overlay._bar.style_dot_rect(overlay)
        assert popover.bottom() + tokens.BarMetric.MENU_OFFSET < bar.top()
        assert popover.center().x() == pytest.approx(dot.center().x(), abs=1)

    def test_it_stays_open_across_picks_and_keys(self, monkeypatch):
        overlay = _styled_overlay()
        overlay._bar.select_tool("rect")
        QTest.mouseClick(overlay._bar._style_dot, Qt.MouseButton.LeftButton)
        popover = overlay._style_popover
        monkeypatch.setattr(QColorDialog, "getColor", staticmethod(lambda *a, **k: QColor("#123456")))

        still_open = []
        for pick in (
            lambda: QTest.mouseClick(
                popover._swatch_buttons[tokens.INK_SWATCHES[2][1]], Qt.MouseButton.LeftButton
            ),
            lambda: QTest.mouseClick(popover._fill_button, Qt.MouseButton.LeftButton),
            lambda: QTest.mouseClick(popover._dash_button, Qt.MouseButton.LeftButton),
            lambda: popover._size_slider.setValue(12),
            lambda: QTest.mouseClick(popover._custom_button, Qt.MouseButton.LeftButton),
            lambda: QTest.keyClick(overlay, Qt.Key.Key_5),
            lambda: QTest.keyClick(overlay, Qt.Key.Key_BracketRight),
            lambda: QTest.keyClick(overlay, Qt.Key.Key_D),
        ):
            pick()
            still_open.append(popover.isVisible())

        assert still_open == [True] * 8
        assert overlay._bar._style_dot.is_open

    def test_what_it_shows_follows_each_pick(self):
        overlay = _styled_overlay()
        overlay._bar.select_tool("pen")
        QTest.mouseClick(overlay._bar._style_dot, Qt.MouseButton.LeftButton)
        popover = overlay._style_popover

        QTest.keyClick(overlay, Qt.Key.Key_3)
        QTest.keyClick(overlay, Qt.Key.Key_BracketRight)

        _name, hex_colour = tokens.INK_SWATCHES[2]
        assert popover._swatch_buttons[hex_colour].is_selected
        assert popover._size_slider.value() == overlay._styles.of("pen").size
        assert popover._size_readout.text() == f"{overlay._styles.of('pen').size}px"

    def test_a_tools_style_survives_a_switch_to_another_and_back(self):
        overlay = _styled_overlay()
        overlay._bar.select_tool("rect")
        QTest.mouseClick(overlay._bar._style_dot, Qt.MouseButton.LeftButton)
        popover = overlay._style_popover
        _name, violet = tokens.INK_SWATCHES[4]
        QTest.mouseClick(popover._swatch_buttons[violet], Qt.MouseButton.LeftButton)
        QTest.mouseClick(popover._fill_button, Qt.MouseButton.LeftButton)
        popover._size_slider.setValue(14)
        rect = overlay._styles.of("rect")
        pen = overlay._styles.of("pen")

        QTest.keyClick(overlay, Qt.Key.Key_P)
        on_the_pen = (popover.tool, overlay._bar._style_dot.style)
        QTest.keyClick(overlay, Qt.Key.Key_R)

        assert on_the_pen == ("pen", pen)
        assert overlay._styles.of("rect") == rect
        assert (rect.colour, rect.size) == (violet, 14)
        assert popover._swatch_buttons[violet].is_selected
        assert popover._fill_button.state == rect.fill
        assert overlay._bar._style_dot.style == rect

    def test_a_fill_and_line_pick_reach_the_next_mark_drawn(self):
        overlay = _styled_overlay()
        overlay._bar.select_tool("ellipse")
        QTest.mouseClick(overlay._bar._style_dot, Qt.MouseButton.LeftButton)
        QTest.mouseClick(overlay._style_popover._fill_button, Qt.MouseButton.LeftButton)
        QTest.mouseClick(overlay._style_popover._dash_button, Qt.MouseButton.LeftButton)
        # Closed first: a press on the frame while it is open only closes it.
        QTest.keyClick(overlay, Qt.Key.Key_Escape)

        QTest.mousePress(overlay, Qt.MouseButton.LeftButton, pos=QPoint(450, 250))
        QTest.mouseMove(overlay, QPoint(600, 330))
        QTest.mouseRelease(overlay, Qt.MouseButton.LeftButton, pos=QPoint(600, 330))

        mark = overlay.marks[-1]
        assert isinstance(mark, Ellipse)
        assert (mark.fill, mark.dash) == ("filled", "dashed")
        assert mark.colour == QColor(tokens.DEFAULT_STYLE["ellipse"]["color"])
        assert mark.stroke_width == tokens.DEFAULT_STYLE["ellipse"]["size"]

    def test_a_strength_reaches_the_next_redaction(self):
        overlay = _styled_overlay()
        overlay._bar.select_tool("blur")
        QTest.mouseClick(overlay._bar._style_dot, Qt.MouseButton.LeftButton)
        overlay._style_popover._strength_slider.setValue(17)
        QTest.keyClick(overlay, Qt.Key.Key_Escape)

        QTest.mousePress(overlay, Qt.MouseButton.LeftButton, pos=QPoint(450, 250))
        QTest.mouseMove(overlay, QPoint(600, 330))
        QTest.mouseRelease(overlay, Qt.MouseButton.LeftButton, pos=QPoint(600, 330))

        assert overlay.marks[-1].strength == 17

    def test_a_key_to_another_tool_keeps_it_open_for_that_tool(self):
        # A key changes the tool without dismissing anything, so the popover
        # follows it, and is placed again for its new height.
        overlay = _styled_overlay()
        overlay._bar.select_tool("blur")
        QTest.mouseClick(overlay._bar._style_dot, Qt.MouseButton.LeftButton)
        popover = overlay._style_popover

        QTest.keyClick(overlay, Qt.Key.Key_P)

        assert popover.isVisible()
        assert popover.sections() == ["color", "size"]
        assert popover.geometry().bottom() < overlay._bar.geometry().top()

    def test_a_key_to_a_tool_with_nothing_to_style_closes_it(self):
        overlay = _styled_overlay()
        overlay._bar.select_tool("pen")
        QTest.mouseClick(overlay._bar._style_dot, Qt.MouseButton.LeftButton)

        QTest.keyClick(overlay, Qt.Key.Key_E)

        assert not overlay._style_popover.isVisible()
        assert not overlay._bar._style_dot.is_open

    def test_clicking_another_tool_closes_it(self):
        overlay = _styled_overlay()
        overlay._bar.select_tool("pen")
        QTest.mouseClick(overlay._bar._style_dot, Qt.MouseButton.LeftButton)

        QTest.mouseClick(overlay._bar._tool_buttons["text"], Qt.MouseButton.LeftButton)

        assert not overlay._style_popover.isVisible()
        assert overlay._tool_hint.isVisible()

    def test_escape_closes_it_before_anything_else(self):
        overlay = _styled_overlay()
        overlay._bar.select_tool("pen")
        QTest.mouseClick(overlay._bar._style_dot, Qt.MouseButton.LeftButton)
        selection = QRect(overlay._selection)

        QTest.keyClick(overlay, Qt.Key.Key_Escape)

        assert not overlay._style_popover.isVisible()
        assert overlay._selection == selection

    def test_the_tool_hint_gives_way_to_it_and_comes_back(self):
        overlay = _styled_overlay()
        overlay._bar.select_tool("pen")
        assert overlay._tool_hint.isVisible()

        QTest.mouseClick(overlay._bar._style_dot, Qt.MouseButton.LeftButton)
        while_open = overlay._tool_hint.isVisible()
        overlay._bar._tool_buttons["eraser"].hovered.emit("eraser")
        on_hover = overlay._tool_hint.isVisible()
        QTest.mouseClick(overlay._bar._style_dot, Qt.MouseButton.LeftButton)

        assert (while_open, on_hover) == (False, False)
        assert overlay._tool_hint.isVisible()

    def test_it_hides_when_the_selection_is_cleared(self):
        overlay = _styled_overlay()
        overlay._bar.select_tool("pen")
        QTest.mouseClick(overlay._bar._style_dot, Qt.MouseButton.LeftButton)

        overlay.set_selection(None)

        assert not overlay._style_popover.isVisible()
        assert not overlay._bar._style_dot.is_open

    def test_it_stays_down_while_the_overlay_itself_is_not_shown(self):
        # None of this file's pixel-sampling OverlayWindow tests call
        # .show(), so the popover may not start painting into a grab() they
        # did not ask for.
        frame = make_frame(image_size=(200, 200), logical_size=(200, 200))
        overlay = OverlayWindow(frame)
        overlay.set_selection(QRect(50, 50, 50, 50))

        overlay._on_tool_selected("pen")
        overlay._toggle_style()

        assert overlay._style_popover.isHidden()

    def test_a_click_on_its_slider_leaves_the_keys_with_the_window(self):
        overlay = _styled_overlay()
        overlay._bar.select_tool("pen")
        QTest.mouseClick(overlay._bar._style_dot, Qt.MouseButton.LeftButton)
        slider = overlay._style_popover._size_slider

        QTest.mouseClick(slider, Qt.MouseButton.LeftButton, pos=QPoint(slider.width() - 3, slider.height() // 2))
        QTest.keyClick(overlay, Qt.Key.Key_6)

        assert overlay.focusWidget() is not slider
        assert overlay._styles.of("pen").colour == tokens.INK_SWATCHES[5][1]

    def test_the_style_is_there_on_the_next_snip(self):
        # Remembered for the session: every overlay is built fresh, and the
        # style set on one is still set on the next.
        first = _styled_overlay()
        first._bar.select_tool("pen")
        QTest.keyClick(first, Qt.Key.Key_3)
        first.close()

        second = _styled_overlay()
        second._bar.select_tool("pen")

        assert second._styles is session_styles
        assert second._styles.of("pen").colour == tokens.INK_SWATCHES[2][1]
        assert second._bar._style_dot.style.colour == tokens.INK_SWATCHES[2][1]


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
        monkeypatch.setattr(output_module, "copy_image_to_clipboard", lambda image: None)
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

    def test_pick_color_at_shows_what_was_copied(self, monkeypatch):
        monkeypatch.setattr(output_module, "copy_text_to_clipboard", lambda text: None)
        overlay = self._overlay()
        overlay._frame.image.fill(QColor(0x3B, 0x82, 0xF6))

        overlay.pick_color_at(QPointF(50, 50))

        assert overlay._toast.isVisible()
        assert overlay._toast._text_label.text() == "Copied #3b82f6"

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
        monkeypatch.setattr(output_module, "copy_image_to_clipboard", lambda image: None)
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
        monkeypatch.setattr(output_module, "copy_image_to_clipboard", lambda image: None)
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
        # Window arms only where a provider can answer; without one it falls
        # back to Region before the label is ever checked.
        overlay._geometry_provider = _FakeWindowProvider(QRectF(0, 0, 100, 100))
        overlay.show()
        QTest.qWaitForWindowExposed(overlay)
        overlay.set_selection(QRect(400, 200, 200, 150))
        QTest.mouseClick(overlay._bar._chip, Qt.MouseButton.LeftButton)
        # Window, because it arms rather than capturing -- this test is only
        # about the generic "picking a row records the label and updates the
        # chip" mechanism every row shares, and Full screen or Browser would
        # finish the snip underneath it.
        target_label = "Window"

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


class TestFamilyMenuComposition:
    """A family's menu carries every sibling, in the family's own order."""

    def test_the_shapes_menu_lists_the_handoffs_shapes_and_no_crop(self):
        # #78: Crop was a fifth row, and it drew a dashed box that cropped
        # nothing. Callout joined afterwards, named as a shape sibling by
        # docs/design/bars/README.md:78.
        menu = FamilyMenu("shapes")

        assert list(menu._rows) == ["rect", "ellipse", "line", "arrow", "callout"]
        assert "crop" not in tokens.TOOLS
        assert menu.width() == tokens.BarMetric.MENU_W_SHAPES

    def test_each_shape_row_carries_its_key(self):
        menu = FamilyMenu("shapes")

        keys = {tool: row._shortcut for tool, row in menu._rows.items()}

        assert keys == {
            "rect": "R", "ellipse": "O", "line": "L", "arrow": "A", "callout": "C",
        }

    def test_the_redaction_menu_says_what_each_one_guarantees(self):
        menu = FamilyMenu("redact")

        assert list(menu._rows) == ["blur", "pixelate", "blackout", "spotlight"]
        assert menu.width() == tokens.BarMetric.MENU_W_REDACT
        assert [row._note for row in menu._rows.values()] == [
            "Softens it — shapes still readable",
            "Blocky, obviously deliberate",
            "Solid bar. Nothing to reconstruct",
            "Dims everything else, not this",
        ]

    @pytest.mark.parametrize(
        "family,width",
        [
            ("shapes", tokens.BarMetric.MENU_W_SHAPES),
            ("redact", tokens.BarMetric.MENU_W_REDACT),
        ],
    )
    def test_its_width_is_the_whole_menu_border_included(self, family, width):
        # The handoff's own warning: its style popover, authored at 216px,
        # rendered 238px once padding and border were added outside it.
        menu = FamilyMenu(family)
        menu.resize(menu.sizeHint())

        assert menu.grab().deviceIndependentSize().width() == width


class TestFamilyMenuSelection:
    def test_the_first_sibling_is_ticked_by_default(self):
        menu = FamilyMenu("shapes")

        assert menu.current == "rect"
        assert menu._rows["rect"].is_selected
        assert not any(row.is_selected for tool, row in menu._rows.items() if tool != "rect")

    def test_clicking_a_row_ticks_it_names_it_and_closes(self):
        menu = FamilyMenu("redact")
        menu.show()
        received = Mock()
        menu.siblingPicked.connect(received)

        QTest.mouseClick(menu._rows["blackout"], Qt.MouseButton.LeftButton)

        received.assert_called_once_with("blackout")
        assert menu.current == "blackout"
        assert not menu.isVisible()


class TestFamilyMenuOverlayIntegration:
    """Each notched slot's menu, wired into `OverlayWindow`."""

    def _overlay(self, selection=QRect(400, 200, 400, 300)):
        frame = make_frame(image_size=(1600, 1000), logical_size=(1600, 1000))
        overlay = OverlayWindow(frame)
        overlay.show()
        QTest.qWaitForWindowExposed(overlay)
        overlay.set_selection(selection)
        return overlay

    @staticmethod
    def _notch(overlay, family: str) -> FamilyMenu:
        QTest.mouseClick(overlay._bar._tool_buttons[family].notch, Qt.MouseButton.LeftButton)
        return overlay._family_menus[family]

    @pytest.mark.parametrize("family", ["shapes", "redact"])
    def test_a_notch_opens_its_menu(self, family):
        overlay = self._overlay()

        menu = self._notch(overlay, family)

        assert menu.isVisible()

    def test_the_notch_again_closes_it(self):
        overlay = self._overlay()
        self._notch(overlay, "shapes")

        menu = self._notch(overlay, "shapes")

        assert not menu.isVisible()

    def test_a_right_click_on_the_slot_opens_it_too(self):
        overlay = self._overlay()

        overlay._bar._tool_buttons["redact"].rightClicked.emit()

        assert overlay._family_menus["redact"].isVisible()

    def test_one_menu_is_open_at_a_time(self):
        overlay = self._overlay()
        self._notch(overlay, "shapes")

        self._notch(overlay, "redact")

        assert overlay._family_menus["redact"].isVisible()
        assert not overlay._family_menus["shapes"].isVisible()

    def test_picking_a_sibling_arms_it_and_the_slot_shows_it(self):
        overlay = self._overlay()
        menu = self._notch(overlay, "shapes")

        QTest.mouseClick(menu._rows["arrow"], Qt.MouseButton.LeftButton)

        assert overlay._bar.active_tool == "arrow"
        assert not menu.isVisible()
        slot = overlay._bar._tool_buttons["shapes"]
        assert slot._icon_name == "arrow"
        assert slot.is_active

    def test_callout_is_a_shapes_sibling_picked_like_the_others(self):
        overlay = self._overlay()
        menu = self._notch(overlay, "shapes")
        assert "callout" in menu._rows

        QTest.mouseClick(menu._rows["callout"], Qt.MouseButton.LeftButton)

        assert overlay._bar.active_tool == "callout"
        assert not menu.isVisible()
        slot = overlay._bar._tool_buttons["shapes"]
        assert slot._icon_name == "callout"
        assert slot.is_active

    def test_the_slot_arms_whichever_sibling_was_used_last(self):
        overlay = self._overlay()
        QTest.mouseClick(
            self._notch(overlay, "shapes")._rows["ellipse"], Qt.MouseButton.LeftButton
        )
        QTest.mouseClick(overlay._bar._tool_buttons["pen"], Qt.MouseButton.LeftButton)
        assert overlay._bar._tool_buttons["shapes"]._icon_name == "ellipse"

        QTest.mouseClick(overlay._bar._tool_buttons["shapes"], Qt.MouseButton.LeftButton)

        assert overlay._bar.active_tool == "ellipse"
        assert not overlay._family_menus["shapes"].isVisible(), "a click uses the slot"

    def test_reopening_ticks_the_sibling_the_slot_shows(self):
        overlay = self._overlay()
        overlay._bar.select_tool("line")

        menu = self._notch(overlay, "shapes")

        assert menu._rows["line"].is_selected

    def test_clicking_a_tool_closes_an_open_menu(self):
        overlay = self._overlay()
        menu = self._notch(overlay, "redact")

        QTest.mouseClick(overlay._bar._tool_buttons["text"], Qt.MouseButton.LeftButton)

        assert not menu.isVisible()
        assert overlay._bar.active_tool == "text"

    def test_clicking_outside_closes_it_and_does_nothing_else(self):
        overlay = self._overlay()
        menu = self._notch(overlay, "shapes")
        original = overlay._bar.active_tool

        # On the dimmed frame, where a press would otherwise start a new
        # selection.
        QTest.mouseClick(overlay, Qt.MouseButton.LeftButton, pos=QPoint(2, 2))

        assert not menu.isVisible()
        assert overlay._bar.active_tool == original
        assert overlay._selection == QRect(400, 200, 400, 300)

    def test_esc_closes_an_open_menu_and_nothing_else(self):
        overlay = self._overlay()
        overlay.add_mark(
            Rectangle(
                colour=QColor(255, 0, 0), stroke_width=4,
                start=QPointF(450, 250), end=QPointF(500, 300),
            )
        )
        menu = self._notch(overlay, "redact")

        QTest.keyClick(overlay, Qt.Key.Key_Escape)

        assert not menu.isVisible()
        assert len(overlay.marks) == 1
        assert overlay._selection == QRect(400, 200, 400, 300)

    def test_it_opens_above_the_bar_centred_on_its_slot(self):
        overlay = self._overlay()

        menu = self._notch(overlay, "shapes")

        bar = QRectF(overlay._bar.geometry())
        slot = QRectF(overlay._bar.slot_rect("shapes", overlay))
        geometry = QRectF(menu.geometry())
        assert geometry.bottom() == bar.top() - tokens.BarMetric.MENU_OFFSET
        assert geometry.center().x() == pytest.approx(slot.center().x(), abs=1)

    def test_it_opens_below_the_bar_when_there_is_no_room_above(self):
        overlay = self._overlay(selection=QRect(400, 10, 400, 40))

        menu = self._notch(overlay, "shapes")

        bar = QRectF(overlay._bar.geometry())
        geometry = QRectF(menu.geometry())
        assert geometry.top() == bar.bottom() + tokens.BarMetric.MENU_OFFSET
        assert overlay._chrome_bounds().contains(geometry)

    def test_it_hides_when_the_overlay_is_hidden(self):
        overlay = self._overlay()
        menu = self._notch(overlay, "shapes")

        overlay.hide()

        assert not menu.isVisible()

    def test_it_hides_when_the_selection_is_cleared(self):
        overlay = self._overlay()
        menu = self._notch(overlay, "redact")

        overlay.set_selection(None)

        assert not menu.isVisible()


class TestFamilyMenuRowsStayClickable:
    """The tool hint strip gives way to an open family menu.

    Hovering a slot shows the strip just under the bar. With the bar above
    the selection a family menu opens below the bar as well, and the strip
    sat on top of its first row: a click on Rectangle landed on the strip,
    the menu stayed open and the shape stayed what it was -- reported as not
    being able to switch shape until another tool had been picked first.
    Driven through the window, hover first, the way a pointer arrives.
    """

    @pytest.fixture(autouse=True)
    def _clean_slate(self):
        _close_stray_toplevel_windows()

    def _overlay(self, placement):
        # The whole offscreen screen, so every point is on a real widget at
        # any scale factor.
        screen = QGuiApplication.primaryScreen().geometry()
        width, height = screen.width(), screen.height()
        overlay = OverlayWindow(make_frame(image_size=(width, height), logical_size=(width, height)))
        overlay.setGeometry(0, 0, width, height)
        overlay.show()
        QTest.qWaitForWindowExposed(overlay)
        margin = height // 10
        if placement == "below":
            overlay.set_selection(QRect(margin, margin, width - 2 * margin, height // 3))
        else:
            top = height // 3
            overlay.set_selection(QRect(margin, top, width - 2 * margin, height - top - 4))
        QApplication.processEvents()
        bar, selection = overlay._bar.geometry(), overlay._selection
        if placement == "below":
            assert bar.top() > selection.bottom()
        else:
            assert bar.bottom() < selection.top()
        return overlay

    @staticmethod
    def _move_to(overlay, widget):
        point = widget.mapTo(overlay, widget.rect().center())
        QTest.mouseMove(overlay.windowHandle(), point)
        QApplication.processEvents()
        return point

    def _click(self, overlay, widget):
        point = self._move_to(overlay, widget)
        QTest.mouseClick(
            overlay.windowHandle(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, point
        )
        QApplication.processEvents()

    @pytest.mark.parametrize("placement", ["below", "above"])
    def test_every_shape_row_can_be_picked_after_hovering_the_slot(self, placement):
        overlay = self._overlay(placement)
        bar = overlay._bar
        slot = bar._tool_buttons["shapes"]
        menu = overlay._family_menus["shapes"]

        for tool in ("ellipse", "rect", "line", "arrow", "rect"):
            self._move_to(overlay, slot)
            self._click(overlay, slot._notch)
            assert not menu.isHidden()
            assert overlay._tool_hint.isHidden()
            row = menu._rows[tool]
            hit = overlay.childAt(row.mapTo(overlay, row.rect().center()))
            assert hit is row or row.isAncestorOf(hit), f"{tool} row is under {type(hit).__name__}"

            self._click(overlay, row)

            assert bar.active_tool == tool
            assert menu.isHidden()
            assert overlay._tool_hint.isVisible()

    def test_hovering_the_slots_with_a_menu_open_keeps_the_hint_away(self):
        overlay = self._overlay("above")
        bar = overlay._bar
        self._click(overlay, bar._tool_buttons["redact"]._notch)
        assert not overlay._family_menus["redact"].isHidden()

        for slot in bar._tool_buttons.values():
            self._move_to(overlay, slot)
            assert overlay._tool_hint.isHidden()

    def test_closing_the_menu_brings_the_hint_back(self):
        overlay = self._overlay("above")
        notch = overlay._bar._tool_buttons["shapes"]._notch
        self._click(overlay, notch)
        assert overlay._tool_hint.isHidden()

        self._click(overlay, notch)

        assert overlay._family_menus["shapes"].isHidden()
        assert overlay._tool_hint.isVisible()


class TestAnArmedFamilySlotOpensItsMenu:
    """A click on a shape or redaction slot that is already armed opens its
    menu, and the next one closes it (#77).

    Reported as having to click in one exact place for anything to open:
    the menu answered only to a 9px triangle in the slot's corner, and a
    click anywhere else armed the shape the slot already had. Driven through
    the window, hover first, the way a pointer arrives.
    """

    @pytest.fixture(autouse=True)
    def _clean_slate(self):
        _close_stray_toplevel_windows()

    def _overlay(self):
        # The whole offscreen screen, so every point is on a real widget at
        # any scale factor.
        screen = QGuiApplication.primaryScreen().geometry()
        width, height = screen.width(), screen.height()
        overlay = OverlayWindow(make_frame(image_size=(width, height), logical_size=(width, height)))
        overlay.setGeometry(0, 0, width, height)
        overlay.show()
        QTest.qWaitForWindowExposed(overlay)
        margin = height // 10
        overlay.set_selection(QRect(margin, margin, width - 2 * margin, height // 3))
        QApplication.processEvents()
        return overlay

    @staticmethod
    def _click(overlay, widget, local=None):
        point = widget.mapTo(overlay, widget.rect().center() if local is None else local)
        QTest.mouseMove(overlay.windowHandle(), point)
        QApplication.processEvents()
        QTest.mouseClick(
            overlay.windowHandle(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, point
        )
        QApplication.processEvents()

    def test_rectangle_then_ellipse_from_the_menu_then_the_menu_again(self):
        overlay = self._overlay()
        bar = overlay._bar
        slot = bar._tool_buttons["shapes"]
        menu = overlay._family_menus["shapes"]
        self._click(overlay, slot)
        assert bar.active_tool == "rect"
        assert menu.isHidden(), "the first click arms the shape the slot shows"
        self._click(overlay, slot)
        assert not menu.isHidden()
        self._click(overlay, menu._rows["ellipse"])
        assert bar.active_tool == "ellipse"
        assert menu.isHidden()

        self._click(overlay, slot)

        assert not menu.isHidden()
        assert bar.active_tool == "ellipse"

    @pytest.mark.parametrize("family", ["shapes", "redact"])
    def test_each_click_on_the_armed_slot_opens_or_closes_its_menu(self, family):
        overlay = self._overlay()
        bar = overlay._bar
        slot = bar._tool_buttons[family]
        menu = overlay._family_menus[family]
        self._click(overlay, slot)
        armed = bar.active_tool

        opened = []
        for _ in range(3):
            self._click(overlay, slot)
            opened.append(not menu.isHidden())

        assert opened == [True, False, True]
        assert bar.active_tool == armed

    def test_the_click_that_opens_the_menu_closes_the_style_popover(self):
        overlay = self._overlay()
        bar = overlay._bar
        slot = bar._tool_buttons["shapes"]
        self._click(overlay, slot)
        self._click(overlay, bar._style_dot)
        assert not overlay._style_popover.isHidden()

        self._click(overlay, slot)

        assert overlay._style_popover.isHidden()
        assert not overlay._family_menus["shapes"].isHidden()

    def test_a_press_just_off_the_triangle_still_opens_the_menu(self):
        overlay = self._overlay()
        bar = overlay._bar
        slot = bar._tool_buttons["shapes"]
        armed = bar.active_tool
        side = tokens.BarMetric.BTN

        # Inside the corner the notch answers to, outside the 9px box its
        # triangle is drawn in.
        self._click(overlay, slot, QPoint(side - 11, side - 11))

        assert not overlay._family_menus["shapes"].isHidden()
        assert bar.active_tool == armed


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

    @pytest.mark.parametrize("family", ["shapes", "redact"])
    def test_a_family_menus_height_equals_its_rows_plus_padding(self, family):
        menu = FamilyMenu(family)
        menu.resize(menu.sizeHint())
        menu.grab()
        metric = tokens.BarMetric

        children_height = sum(row.height() for row in menu._rows.values())

        assert menu.height() == children_height + 2 * (metric.MENU_PAD + metric.BORDER)

    @pytest.mark.parametrize("family", ["shapes", "redact"])
    def test_a_family_menus_rows_are_not_collapsed(self, family):
        menu = FamilyMenu(family)
        pad_v, _pad_h = tokens.BarMetric.MENU_ROW_PAD

        assert all(
            row.sizeHint().height() >= tokens.BarMetric.MENU_ROW_ICON + 2 * pad_v
            for row in menu._rows.values()
        )

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
    (`CaptureModePopover`/`FamilyMenu` both `setFixedWidth`), the same
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

    @pytest.mark.parametrize("family", ["shapes", "redact"])
    def test_a_family_menu_opens_with_no_child_clipped(self, family):
        menu = FamilyMenu(family)
        menu.resize(menu.sizeHint())
        menu.grab()

        self._assert_no_child_is_clipped(menu)

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

    # y=90, not 30: while `_picking_window` is armed the chooser row stays
    # up (`_sync_chooser_visibility`), and it and its hint pill take the top
    # BarMetric.ROW_H + HINT_GAP + the pill's 22px (71px) of the monitor's
    # centre. A rect/point inside that band never reaches
    # `OverlayWindow.mouseMoveEvent` at all (Qt delivers a bare hover to
    # whichever child sits under the cursor instead), silently turning a
    # hit into a no-op. 90 clears it with margin.
    WINDOW_RECT = QRectF(30, 90, 50, 50)
    HIT_POINT = QPoint(50, 110)
    # Outside WINDOW_RECT, and -- as important -- outside every child widget
    # `OverlayWindow` shows while a window is being picked: the chooser row
    # and its hint pill across the top centre, the floating bar and tool hint
    # under the previewed window, and the close button in the corner. A point
    # inside any of them never reaches `OverlayWindow.mouseMoveEvent` at all
    # (Qt delivers it to that child instead), silently turning a miss into a
    # no-op. The hint pill's width follows the UI font: (200, 70) was clear of
    # it in Ubuntu's font and inside it in DejaVu Sans, which is what CI has,
    # so the test checks the point is clear before relying on it. It also has
    # to be on the screen: the offscreen platform's is 533px wide at 1.5x,
    # and a move past its edge is never delivered.
    MISS_POINT = QPoint(500, 300)
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
        assert overlay.childAt(self.MISS_POINT) is None, "the miss point is under chrome"

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
        monkeypatch.setattr(output_module, "copy_image_to_clipboard", copied.append)
        overlay = self._overlay()

        self._drag(overlay, QPoint(100, 100), QPoint(400, 350))

        assert len(copied) == 1
        assert copied[0].size() == QSize(300, 250)
        assert not overlay.isVisible()

    def test_nothing_is_copied_part_way_through_the_drag(self, monkeypatch):
        # The whole reason this hangs off a commit funnel and not
        # `set_selection`, which runs on every move of a live drag.
        copied = []
        monkeypatch.setattr(output_module, "copy_image_to_clipboard", copied.append)
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
        monkeypatch.setattr(output_module, "copy_image_to_clipboard", copied.append)
        overlay = self._overlay()

        self._drag(overlay, QPoint(100, 100), QPoint(105, 104))

        assert copied == []
        assert overlay.isVisible()

    def test_picking_a_window_copies_it_immediately(self, monkeypatch):
        copied = []
        monkeypatch.setattr(output_module, "copy_image_to_clipboard", copied.append)
        overlay = self._overlay()
        overlay._chooser.set_mode("Window")

        QTest.mouseClick(overlay, Qt.MouseButton.LeftButton, pos=QPoint(100, 100))

        assert len(copied) == 1
        assert not overlay.isVisible()

    def test_full_screen_copies_without_a_click_at_all(self, monkeypatch):
        copied = []
        monkeypatch.setattr(output_module, "copy_image_to_clipboard", copied.append)
        overlay = self._overlay()

        overlay._chooser.set_mode("Full screen")

        assert len(copied) == 1
        assert not overlay.isVisible()

    def test_edit_leaves_the_frame_up_with_the_bar_on_it(self, monkeypatch):
        # The default, and the behaviour every version before this had.
        copied = []
        monkeypatch.setattr(output_module, "copy_image_to_clipboard", copied.append)
        overlay = self._overlay(outcome="edit")

        self._drag(overlay, QPoint(100, 100), QPoint(400, 350))

        assert copied == []
        assert overlay.isVisible()
        assert overlay._bar.isVisibleTo(overlay)

    def test_review_leaves_the_frame_up_too(self, monkeypatch):
        # `review` is about what opens *after* the overlay, so the overlay
        # itself behaves exactly as `edit` does.
        copied = []
        monkeypatch.setattr(output_module, "copy_image_to_clipboard", copied.append)
        overlay = self._overlay(outcome="review")

        self._drag(overlay, QPoint(100, 100), QPoint(400, 350))

        assert copied == []
        assert overlay.isVisible()

    def test_it_reports_the_capture_the_same_way_copy_always_has(self, monkeypatch):
        # `app.py` opens the review window off this hook; instant is the
        # ordinary Copy path, so it reports like one.
        monkeypatch.setattr(output_module, "copy_image_to_clipboard", lambda image: None)
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
        monkeypatch.setattr(output_module, "copy_image_to_clipboard", copied.append)
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
        # The stills/record side is not remembered at all, so a
        # `set_kind("record")` below reaches nothing outside this overlay.

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

    def test_a_delay_picked_on_the_chooser_row_reaches_the_callback(self):
        # #73: the row is where a recording's delay is chosen -- there is no
        # floating bar on the record side to pick one from -- and its value
        # used to stop at the row.
        requests = []
        overlay = self._overlay(
            on_recording_requested=lambda rect, delay, after: requests.append(delay)
        )
        overlay._chooser.set_delay(tokens.DELAYS[1])

        self._drag(overlay, QPoint(100, 100), QPoint(400, 350))

        assert requests == [tokens.DELAYS[1]]

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
    needed past picking the row -- on a desk with one monitor. With more it
    arms, previews the monitor under the pointer and takes the one clicked
    (#53).
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

    def test_with_two_monitors_it_previews_the_one_the_cursor_last_moved_over(self):
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

        assert overlay._selection is None
        assert overlay._picking_monitor
        assert overlay._hovered_monitor == left

        QTest.mouseClick(overlay, Qt.MouseButton.LeftButton, pos=QPoint(100, 300))

        assert overlay._selection == left.toRect()

    def test_with_no_prior_cursor_move_the_preview_opens_where_the_os_has_the_pointer(
        self, monkeypatch
    ):
        # No move has reached the overlay, so the OS's own pointer decides:
        # the monitor the chooser row opened on. Deliberately the *left*
        # one -- the window's centre (300, 300) is inside `right`, which is
        # what this took before #49, wherever the pointer was.
        left = QRectF(0, 0, 250, 600)
        right = QRectF(250, 0, 350, 600)
        _point_the_os_at(monkeypatch, left)
        overlay = self._overlay(monitor_geometries=[left, right])

        self._pick_full_screen(overlay)

        assert overlay._hovered_monitor == left

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
    # Window arms rather than capturing, so it proves a mode's own dispatch
    # ran without the snip finishing underneath the assertions.
    WINDOW_LABEL = "Window"

    def _overlay(self, registry=None, size=(600, 600)):
        frame = make_frame(image_size=size, logical_size=size)
        # A provider that can answer, or Window falls back to Region.
        overlay = OverlayWindow(
            frame,
            registry=registry,
            geometry_provider=_FakeWindowProvider(QRectF(0, 0, 100, 100)),
        )
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
        QTest.mouseClick(overlay._bar._style_dot, Qt.MouseButton.LeftButton)
        _name, target_hex = tokens.INK_SWATCHES[3]
        QTest.mouseClick(
            overlay._style_popover._swatch_buttons[target_hex], Qt.MouseButton.LeftButton
        )
        overlay._style_popover._size_slider.setValue(21)
        QTest.keyClick(overlay, Qt.Key.Key_Escape)  # closes the popover, nothing more
        self._open_popover_and_set_delay(overlay)
        self._confirm_mode(overlay, self.REGION_LABEL)

        for _ in range(3):
            overlay._delay_timer.timeout.emit()

        assert overlay._bar.active_tool == "pen"
        assert overlay._styles.of("pen").colour == target_hex
        assert overlay._styles.of("pen").size == 21
        assert overlay._bar._style_dot.style == overlay._styles.of("pen")

    def test_delayed_window_pick_still_arms_window_picking_on_the_new_frame(self):
        # The picked mode isn't forgotten across the wait -- Window and Full
        # screen still do their own thing against the fresh frame, same as
        # they would with no delay at all.
        regrabbed_frame = make_frame(image_size=(600, 600), logical_size=(600, 600))
        registry = BackendRegistry([_FakeCaptureBackend(regrabbed_frame)])
        overlay = self._overlay(registry=registry)
        self._open_popover_and_set_delay(overlay)
        self._confirm_mode(overlay, self.WINDOW_LABEL)

        for _ in range(3):
            overlay._delay_timer.timeout.emit()

        assert overlay._picking_window

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
        self._confirm_mode(overlay, self.WINDOW_LABEL)

        assert overlay.isVisible()
        assert overlay._countdown is None
        # Window's own immediate arming (SNX-48) still ran -- proving this
        # went through the ordinary dispatch path, not a delay that just
        # happened to finish instantly.
        assert overlay._picking_window


class TestADelayPickedOnTheChooserRowDelaysTheCapture:
    """#73: the chooser row's Delay showed as set and the capture happened
    at once anyway. The overlay kept its own copy of the delay, fed only by
    the floating bar's popover, and every reader used that copy.

    These drive the row itself rather than `set_delay`, because the bug was
    that a value the row held never reached anything.
    """

    @pytest.fixture(autouse=True)
    def _clean_slate(self):
        _close_stray_toplevel_windows()

    def _overlay(self, registry=None, size=(1200, 800)):
        frame = make_frame(image_size=size, logical_size=size)
        # A provider that can answer, so Window arms rather than falling
        # back to Region: it waits for a click, so the snip does not finish
        # underneath the assertions.
        overlay = OverlayWindow(
            frame,
            registry=registry,
            geometry_provider=_FakeWindowProvider(QRectF(0, 0, 100, 100)),
        )
        overlay.setGeometry(0, 0, *size)
        overlay.show()
        QTest.qWaitForWindowExposed(overlay)
        return overlay

    @staticmethod
    def _click(widget):
        QTest.mouseClick(
            widget, Qt.MouseButton.LeftButton,
            pos=QPoint(widget.width() // 2, widget.height() // 2),
        )

    def _pick_delay_on_the_row(self, overlay, delay):
        # Delay is a flag on the row (#66): each click advances one delay.
        for _ in tokens.DELAYS:
            if overlay._chooser.delay == delay:
                return
            self._click(overlay._chooser.row.delay_flag)
        raise AssertionError(f"the delay flag never reached {delay}")

    def _pick_mode_on_the_row(self, overlay, mode):
        self._click(overlay._chooser.row.mode_chip)
        self._click(overlay._chooser._menu._rows[mode])

    def test_picking_a_mode_hides_the_overlay_and_counts_down(self):
        overlay = self._overlay()
        self._pick_delay_on_the_row(overlay, "3s")

        self._pick_mode_on_the_row(overlay, "Window")

        assert not overlay.isVisible()
        assert overlay._countdown is not None and overlay._countdown.isVisible()
        assert overlay._countdown._label.text() == "3"

    def test_the_countdown_ends_in_a_fresh_grab(self):
        regrabbed = make_frame(image_size=(1200, 800), logical_size=(1200, 800))
        overlay = self._overlay(
            registry=BackendRegistry([_FakeCaptureBackend(regrabbed)])
        )
        self._pick_delay_on_the_row(overlay, "3s")
        self._pick_mode_on_the_row(overlay, "Window")

        for _ in range(3):
            overlay._delay_timer.timeout.emit()

        assert overlay._frame is regrabbed
        assert overlay.isVisible()
        assert overlay._picking_window, "the picked mode was lost across the wait"

    def test_the_bars_popover_shows_the_rows_delay(self):
        overlay = self._overlay()

        self._pick_delay_on_the_row(overlay, "5s")

        assert overlay._delay == "5s"
        assert overlay._popover.delay == "5s"

    def test_the_row_shows_the_popovers_delay(self):
        overlay = self._overlay()
        overlay.set_selection(QRect(100, 100, 300, 200))
        QTest.mouseClick(overlay._bar._chip, Qt.MouseButton.LeftButton)

        QTest.mouseClick(overlay._popover._delay_row, Qt.MouseButton.LeftButton)

        assert overlay._delay == tokens.DELAYS[1]
        assert overlay._chooser.delay == tokens.DELAYS[1]

    def test_no_delay_on_the_row_still_takes_the_mode_at_once(self):
        overlay = self._overlay()

        self._pick_mode_on_the_row(overlay, "Window")

        assert overlay.isVisible()
        assert overlay._countdown is None
        assert overlay._picking_window


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
    key names (`Esc`, `Enter`, the tool shortcuts -- Callout's `C` among
    them since it joined the shapes family) set apart from the surrounding
    prose by family and colour, per "Key names are mono in pure white."
    """

    def test_reads_the_full_hint_line(self):
        hud = HintHUD()

        text = "".join(label.text() for label in hud.findChildren(QLabel))

        assert text == (
            "Esc discard ink · Enter copy & dismiss · "
            "P H R O L A C S T B E I pick a tool · drag any edge to re-frame · "
            "arrows nudge · Alt+arrows resize"
            " — the ink stays where you put it"
        )

    def test_key_segments_cover_esc_enter_and_every_tool_shortcut_in_order(self):
        hud = HintHUD()

        key_texts = [text for text, is_key in hud._segments() if is_key]

        assert key_texts == [
            "Esc",
            "Enter",
            "P H R O L A C S T B E I",
            "arrows",
            "Alt+arrows",
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


    @pytest.mark.parametrize(
        "letter,tool", [("R", "rect"), ("O", "ellipse"), ("L", "line"), ("A", "arrow")]
    )
    def test_a_shape_key_reaches_its_sibling_and_the_slot_follows(self, letter, tool):
        overlay = self._overlay()

        QTest.keyClick(overlay, getattr(Qt.Key, f"Key_{letter}"))

        assert overlay._bar.active_tool == tool
        assert overlay._bar._tool_buttons["shapes"]._icon_name == tool

    def test_b_cycles_the_redaction_family(self):
        overlay = self._overlay()

        seen = []
        for _ in range(5):
            QTest.keyClick(overlay, Qt.Key.Key_B)
            seen.append(
                (overlay._bar.active_tool, overlay._bar._tool_buttons["redact"]._icon_name)
            )

        assert seen == [
            ("blur", "blur"),
            ("pixelate", "mask"),
            ("blackout", "blackout"),
            ("spotlight", "eye"),
            ("blur", "blur"),
        ]

    def test_b_from_another_tool_arms_the_sibling_the_slot_shows(self):
        overlay = self._overlay()
        overlay._bar.select_tool("blackout")
        overlay._bar.select_tool("pen")

        QTest.keyClick(overlay, Qt.Key.Key_B)

        assert overlay._bar.active_tool == "blackout"


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


class TestKeyboardNudge:
    """#88 AC: arrows move the selection one logical pixel (Shift: ten);
    Alt+arrow resizes it instead, from the bottom-right corner, stopping at
    the same minimum a drag stops at; movement clamps to the desktop edge
    exactly as a drag does; nothing moves while a text mark is being typed
    into.

    Every assertion here reads `overlay._selection`, a plain `QRect` in
    this window's own logical coordinate space (the class docstring's
    "window coordinates") -- nothing in `_nudge_selection` ever touches an
    image or a physical pixel, so a press moving it by exactly one unit
    here is already one *logical* pixel at any display scale. This file's
    `QT_SCALE_FACTOR=1.5` run (CONTRIBUTING.md) exercises these same
    assertions unchanged, which is what "at 1.0 and 1.5" comes down to for
    a path with no scale factor anywhere in it to get wrong.
    """

    def _overlay(self, size=(400, 400), selection=QRect(100, 100, 150, 100)):
        frame = make_frame(image_size=size, logical_size=size)
        overlay = OverlayWindow(frame)
        overlay.set_selection(selection)
        return overlay

    def test_right_moves_one_logical_pixel(self):
        overlay = self._overlay()

        QTest.keyClick(overlay, Qt.Key.Key_Right)

        sel = overlay._selection
        assert (sel.x(), sel.y()) == (101, 100)
        assert (sel.width(), sel.height()) == (150, 100)

    def test_all_four_arrows_move_one_pixel_each(self):
        overlay = self._overlay()

        QTest.keyClick(overlay, Qt.Key.Key_Left)
        QTest.keyClick(overlay, Qt.Key.Key_Down)
        QTest.keyClick(overlay, Qt.Key.Key_Left)
        QTest.keyClick(overlay, Qt.Key.Key_Up)

        sel = overlay._selection
        assert (sel.x(), sel.y()) == (98, 100)
        assert (sel.width(), sel.height()) == (150, 100)

    def test_shift_arrow_moves_ten(self):
        overlay = self._overlay()

        QTest.keyClick(overlay, Qt.Key.Key_Down, Qt.KeyboardModifier.ShiftModifier)

        sel = overlay._selection
        assert (sel.x(), sel.y()) == (100, 110)

    def test_alt_right_grows_from_the_bottom_right_corner(self):
        overlay = self._overlay()

        QTest.keyClick(overlay, Qt.Key.Key_Right, Qt.KeyboardModifier.AltModifier)

        sel = overlay._selection
        assert (sel.x(), sel.y()) == (100, 100)  # top-left anchor unmoved
        assert (sel.width(), sel.height()) == (151, 100)

    def test_alt_left_shrinks_from_the_bottom_right_corner(self):
        overlay = self._overlay()

        QTest.keyClick(overlay, Qt.Key.Key_Left, Qt.KeyboardModifier.AltModifier)

        sel = overlay._selection
        assert (sel.x(), sel.y()) == (100, 100)
        assert (sel.width(), sel.height()) == (149, 100)

    def test_alt_shift_resizes_by_ten(self):
        overlay = self._overlay()

        QTest.keyClick(
            overlay,
            Qt.Key.Key_Down,
            Qt.KeyboardModifier.AltModifier | Qt.KeyboardModifier.ShiftModifier,
        )

        sel = overlay._selection
        assert (sel.width(), sel.height()) == (150, 110)

    def test_alt_arrow_stops_at_the_same_minimum_a_drag_stops_at(self):
        overlay = self._overlay(selection=QRect(100, 100, 18, 100))

        for _ in range(3):
            QTest.keyClick(overlay, Qt.Key.Key_Left, Qt.KeyboardModifier.AltModifier)

        sel = overlay._selection
        assert sel.x() == 100  # top-left anchor never moves for this handle
        assert sel.width() == tokens.Metric.SEL_MIN_W == 16

    def test_move_is_clamped_at_the_top_left_edge(self):
        overlay = self._overlay(selection=QRect(5, 5, 150, 100))

        for _ in range(20):
            QTest.keyClick(overlay, Qt.Key.Key_Left)
        for _ in range(20):
            QTest.keyClick(overlay, Qt.Key.Key_Up)

        sel = overlay._selection
        assert (sel.x(), sel.y()) == (0, 0)
        assert (sel.width(), sel.height()) == (150, 100)  # size untouched by a plain move

    def test_move_is_clamped_at_the_bottom_right_edge(self):
        overlay = self._overlay(size=(200, 200), selection=QRect(150, 150, 40, 40))

        for _ in range(20):
            QTest.keyClick(overlay, Qt.Key.Key_Right)
        for _ in range(20):
            QTest.keyClick(overlay, Qt.Key.Key_Down)

        sel = overlay._selection
        assert sel.x() == overlay.width() - sel.width() == 160
        assert sel.y() == overlay.height() - sel.height() == 160

    def test_a_selection_cannot_be_walked_off_the_desk(self):
        # A whole desktop's worth of presses in one direction: whatever the
        # window's size, this must still land exactly on the far edge, not
        # past it.
        overlay = self._overlay(size=(150, 150), selection=QRect(0, 0, 50, 50))

        for _ in range(50):
            QTest.keyClick(overlay, Qt.Key.Key_Right, Qt.KeyboardModifier.ShiftModifier)

        sel = overlay._selection
        assert sel.x() + sel.width() == overlay.width()

    def test_the_size_chip_updates_from_a_nudge_without_a_mouse_release(self):
        overlay = self._overlay()
        before, _ = overlay._dimension_chip_texts()
        assert before == "150 × 100"

        QTest.keyClick(overlay, Qt.Key.Key_Right, Qt.KeyboardModifier.AltModifier)

        after, _ = overlay._dimension_chip_texts()
        assert after == "151 × 100"

    def test_nothing_moves_while_typing_into_a_text_mark(self):
        # Mirrors TestKeyboardToolShortcuts's/`?`'s own suppression cases:
        # a QLineEdit given focus via setFocus() is enough for
        # _shortcuts_suppressed() to see, without the window ever being
        # shown.
        overlay = self._overlay()
        before = QRect(overlay._selection)
        label = QLineEdit(overlay)
        label.setFocus()

        QTest.keyClick(overlay, Qt.Key.Key_Right)
        QTest.keyClick(overlay, Qt.Key.Key_Down, Qt.KeyboardModifier.ShiftModifier)
        QTest.keyClick(overlay, Qt.Key.Key_Right, Qt.KeyboardModifier.AltModifier)

        assert overlay._selection == before


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
            output_module, "copy_image_to_clipboard", lambda image: calls.append(image)
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
        monkeypatch.setattr(output_module, "copy_image_to_clipboard", lambda image: None)
        overlay = self._overlay()

        QTest.keyClick(overlay, Qt.Key.Key_Enter)

        assert not overlay.isVisible()

    def test_enter_without_a_selection_does_nothing(self, monkeypatch):
        # Mirrors Overlay's own Enter guard above -- nothing to flatten or
        # copy without a selection yet.
        calls = []
        monkeypatch.setattr(
            output_module, "copy_image_to_clipboard", lambda image: calls.append(image)
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
            output_module, "copy_image_to_clipboard", lambda image: calls.append(image)
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
            output_module, "copy_image_to_clipboard", lambda image: calls.append(image)
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
            output_module, "copy_image_to_clipboard", lambda image: calls.append(image)
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
            output_module, "copy_image_to_clipboard", lambda image: calls.append(image)
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


def _focused_slider(parent: QWidget) -> QSlider:
    """A slider holding the keyboard focus, standing in for any slider.

    Shown first: a child that was never shown cannot take the focus from a
    window that is on screen.
    """
    slider = QSlider(parent)
    slider.show()
    slider.setFocus()
    return slider


class TestKeyboardShortcutSuppression:
    """SNX-47 AC: 'none of these keys fire while a text label is being
    edited or a slider has focus, and the key reaches the focused widget
    instead.' SNX-79 carves Escape and undo/redo out of that suppression --
    see TestEscapeAndUndoRedoBypassSuppression below for those; this class
    now only covers the shortcuts that must keep yielding to a focused
    slider or label.

    A bare `QSlider` stands in for any slider, as a bare `QLineEdit` does for
    a label: the style popover's own sliders never take focus, so that its
    keys keep working while it is open (TestStylePopoverComposition).
    """

    RED = QColor(255, 0, 0)

    def _overlay(self):
        frame = make_frame(image_size=(200, 200), logical_size=(200, 200))
        overlay = OverlayWindow(frame)
        overlay.set_selection(QRect(0, 0, 200, 200))
        return overlay

    def test_tool_letter_does_not_fire_while_a_slider_has_focus(self):
        overlay = self._overlay()
        _focused_slider(overlay)

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
            output_module, "copy_image_to_clipboard", lambda image: calls.append(image)
        )
        overlay = self._overlay()
        overlay.show()
        QTest.qWaitForWindowExposed(overlay)
        _focused_slider(overlay)

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
        slider = _focused_slider(overlay)
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
        _focused_slider(overlay)

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
        _focused_slider(overlay)

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
        _focused_slider(overlay)

        QTest.keyClick(overlay, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)

        assert overlay.marks == ()

    def test_redo_still_fires_while_a_slider_has_focus(self):
        overlay = self._overlay()
        overlay.add_mark(
            Rectangle(colour=self.RED, stroke_width=4, start=QPointF(0, 0), end=QPointF(10, 10))
        )
        overlay.undo()
        _focused_slider(overlay)

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
        _focused_slider(overlay)

        QTest.keyClick(overlay, Qt.Key.Key_H)

        assert overlay._bar.active_tool == before
        assert overlay._bar.active_tool != "highlighter"

    def test_arrow_key_still_reaches_the_slider_after_the_escape_fix(self):
        overlay = self._overlay()
        slider = _focused_slider(overlay)
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


class TestHideSensitiveText:
    """Hide sensitive: solid boxes over emails, cards and keys, placed when
    a selection is made, driven through a fake recognizer so nothing here
    depends on the machine having OCR.

    The frame is 2x its logical size and sits at a non-zero origin, so a
    box computed in the wrong coordinate space lands visibly in the wrong
    place instead of coincidentally in the right one.
    """

    LOGICAL = (1600, 1000)
    ORIGIN = (-1600, 0)
    SELECTION = QRect(100, 50, 400, 300)

    @pytest.fixture(autouse=True)
    def _clean_slate(self):
        _close_stray_toplevel_windows()

    @pytest.fixture
    def recognizer(self, monkeypatch):
        """Replaces the platform's text recognition. `words` is what it
        returns, as (text, x, y, w, h) in the recognised image's pixels."""
        state = SimpleNamespace(available=True, calls=[], words=[
            ("Email:", 0, 20, 30, 30),
            ("bob.smith@gmail.com", 40, 20, 200, 30),
        ])
        current = overlay_module.platform.current

        def recognize_text(image):
            state.calls.append(QImage(image))
            return [[RecognizedWord(text, QRectF(x, y, w, h)) for text, x, y, w, h in state.words]]

        monkeypatch.setattr(current, "recognizes_text", lambda: state.available)
        monkeypatch.setattr(current, "text_recognition_unavailable_reason", lambda: "")
        monkeypatch.setattr(current, "recognize_text", recognize_text)
        return state

    def _overlay(self, *, hide=True):
        size = self.LOGICAL
        frame = make_frame(
            image_size=(size[0] * 2, size[1] * 2), logical_size=size, logical_origin=self.ORIGIN
        )
        overlay = OverlayWindow(
            frame, monitor_geometries=[QRectF(QPointF(*self.ORIGIN), QSizeF(*size))]
        )
        overlay._chooser.set_hide_sensitive(hide)
        return overlay

    def test_the_box_covers_the_value_in_window_coordinates(self, recognizer):
        overlay = self._overlay()

        overlay._commit_selection(self.SELECTION)

        [box] = overlay._marks
        assert isinstance(box, Redact)
        # Image rect (40, 20, 200x30) at 2x, offset by the selection's
        # (100, 50), padded by 2 on every side.
        rect = QRectF(box.start, box.end).normalized()
        pad = OverlayWindow._HIDE_PADDING
        assert rect.left() == pytest.approx(100 + 20 - pad)
        assert rect.top() == pytest.approx(50 + 10 - pad)
        assert rect.width() == pytest.approx(100 + 2 * pad)
        assert rect.height() == pytest.approx(15 + 2 * pad)

    def test_the_label_is_left_visible(self, recognizer):
        overlay = self._overlay()

        overlay._commit_selection(self.SELECTION)

        assert len(overlay._marks) == 1

    def test_the_recognizer_reads_the_selection_at_full_resolution(self, recognizer):
        overlay = self._overlay()

        overlay._commit_selection(self.SELECTION)

        [image] = recognizer.calls
        assert (image.width(), image.height()) == (800, 600)

    def test_one_undo_removes_every_box(self, recognizer):
        recognizer.words = [
            ("bob.smith@gmail.com", 40, 20, 200, 30),
            ("sk-ant-" + "api03-Xq7pL2mNvB9cR4tY8wZ1", 40, 80, 300, 30),
            ("192.168.14.201", 40, 140, 150, 30),
        ]
        overlay = self._overlay()

        overlay._commit_selection(self.SELECTION)
        assert len(overlay._marks) == 3

        overlay.undo()
        assert len(overlay._marks) == 0

    def test_an_instant_copy_already_has_the_value_blacked_out(self, recognizer, monkeypatch):
        copied = []
        monkeypatch.setattr(output_module, "copy_image_to_clipboard", copied.append)
        monkeypatch.setattr(setup_desktop, "load_instant_saves", lambda: False)
        overlay = self._overlay()
        overlay._on_captured = lambda image, path: None
        overlay._chooser.set_after("instant")

        overlay._commit_selection(self.SELECTION)

        [image] = copied
        # Centre of the value, in exported pixels: the recognised rect's own.
        assert image.pixelColor(140, 35) == QColor(0, 0, 0)
        assert image.pixelColor(700, 500) == QColor(BASE_COLOR)

    def test_nothing_is_read_when_the_toggle_is_off(self, recognizer):
        overlay = self._overlay(hide=False)

        overlay._commit_selection(self.SELECTION)

        assert recognizer.calls == []
        assert overlay._marks == ()

    def test_nothing_is_read_on_the_record_side(self, recognizer):
        overlay = self._overlay()
        overlay._chooser.set_kind("record")

        overlay._commit_selection(self.SELECTION)

        assert recognizer.calls == []

    def test_nothing_is_read_when_the_platform_cannot(self, recognizer):
        recognizer.available = False
        overlay = self._overlay()

        overlay._commit_selection(self.SELECTION)

        assert recognizer.calls == []
        assert overlay._marks == ()

    def test_ordinary_text_adds_nothing_and_no_history(self, recognizer):
        recognizer.words = [("Deploy", 10, 10, 60, 20), ("finished", 80, 10, 70, 20)]
        overlay = self._overlay()

        overlay._commit_selection(self.SELECTION)

        assert overlay._marks == ()
        assert not overlay._mark_store.can_undo

    def test_selecting_the_same_text_again_does_not_stack_duplicate_boxes(self, recognizer):
        overlay = self._overlay()

        overlay._commit_selection(self.SELECTION)
        overlay._commit_selection(self.SELECTION)

        assert len(overlay._marks) == 1

    def test_the_same_value_found_by_both_reads_is_one_box_covering_both(self, recognizer):
        # Recognition reads small selections at two sizes; both usually
        # find the same words, a pixel or two apart.
        recognizer.words = [
            ("bob.smith@gmail.com", 40, 20, 200, 30),
            ("bob.smith@gmail.com", 42, 22, 204, 30),
        ]
        overlay = self._overlay()

        overlay._commit_selection(self.SELECTION)

        [box] = overlay._marks
        rect = QRectF(box.start, box.end).normalized()
        pad = OverlayWindow._HIDE_PADDING
        assert rect.left() == pytest.approx(100 + 20 - pad)
        assert rect.right() == pytest.approx(100 + 123 + pad)

    def test_different_values_side_by_side_stay_separate(self, recognizer):
        recognizer.words = [
            ("bob.smith@gmail.com", 40, 20, 200, 30),
            ("192.168.14.201", 260, 20, 150, 30),
        ]
        overlay = self._overlay()

        overlay._commit_selection(self.SELECTION)

        assert len(overlay._marks) == 2

    def test_it_says_how_many_it_hid(self, recognizer):
        recognizer.words = [
            ("bob.smith@gmail.com", 40, 20, 200, 30),
            ("192.168.14.201", 40, 140, 150, 30),
        ]
        overlay = self._overlay()
        said = []
        overlay._show_toast = lambda icon, text: said.append(text)

        overlay._commit_selection(self.SELECTION)

        assert said == ["Hid 2 items"]

    def test_a_recalled_last_region_is_scanned_too(self, recognizer):
        # Last region pre-selects without committing, so without its own
        # scan the two toggles together would hide nothing at all.
        setup_desktop.save_last_region((-1500, 50, 400, 300))
        overlay = self._overlay()
        overlay._chooser.set_reuse_last_region(True)

        overlay._preselect_last_region()

        assert overlay._selection == self.SELECTION
        assert len(overlay._marks) == 1

    def test_a_word_from_the_users_own_list_is_blacked_out(self, recognizer):
        # Nothing about "Acme Corporation" looks sensitive; it is hidden
        # because this user said it is.
        setup_desktop.save_hide_list(["Acme Corporation"], [], [])
        recognizer.words = [("Acme", 40, 20, 90, 30), ("Corporation", 140, 20, 200, 30)]
        overlay = self._overlay()

        overlay._commit_selection(self.SELECTION)

        assert len(overlay._marks) == 1

    def test_a_label_from_the_users_own_list_hides_its_value(self, recognizer):
        setup_desktop.save_hide_list([], ["Employee ID"], [])
        recognizer.words = [
            ("Employee", 40, 20, 90, 30), ("ID:", 140, 20, 40, 30), ("44821", 190, 20, 70, 30),
        ]
        overlay = self._overlay()

        overlay._commit_selection(self.SELECTION)

        [box] = overlay._marks
        # The value only: 190/2 + the selection's own x, less the padding.
        assert QRectF(box.start, box.end).normalized().left() == pytest.approx(
            self.SELECTION.x() + 95 - OverlayWindow._HIDE_PADDING
        )

    def test_an_edit_takes_effect_on_the_next_capture(self, recognizer):
        recognizer.words = [("Acme", 40, 20, 90, 30)]
        overlay = self._overlay()
        overlay._commit_selection(self.SELECTION)
        assert overlay._marks == ()

        setup_desktop.save_hide_list(["Acme"], [], [])
        again = self._overlay()
        again._commit_selection(self.SELECTION)

        assert len(again._marks) == 1

    def test_an_empty_list_changes_nothing(self, recognizer):
        setup_desktop.save_hide_list([], [], [])
        overlay = self._overlay()

        overlay._commit_selection(self.SELECTION)

        # The fixture's own words: the built-in email rule, and nothing else.
        assert len(overlay._marks) == 1

    def test_an_unreadable_list_never_fails_the_capture(self, recognizer, monkeypatch):
        monkeypatch.setattr(
            setup_desktop, "load_hide_list",
            lambda *a, **k: (_ for _ in ()).throw(OSError("unreadable")),
        )
        overlay = self._overlay()

        with pytest.raises(OSError):
            setup_desktop.load_hide_list()
        # The capture itself must still finish: the loader is the only thing
        # allowed to know the file is broken.
        overlay._commit_selection(self.SELECTION)

    def test_the_toggle_is_seeded_from_and_saved_to_config(self, recognizer):
        setup_desktop.save_hide_sensitive(True)
        overlay = OverlayWindow(make_frame())
        assert overlay._chooser.hide_sensitive is True

        QTest.mouseClick(overlay._chooser.row.hide_flag, Qt.MouseButton.LeftButton)

        assert setup_desktop.load_hide_sensitive() is False

    def test_the_toggle_is_greyed_when_the_platform_cannot_read_text(self, monkeypatch):
        current = overlay_module.platform.current
        monkeypatch.setattr(current, "recognizes_text", lambda: False)
        monkeypatch.setattr(current, "text_recognition_unavailable_reason", lambda: "Windows only for now")

        overlay = OverlayWindow(make_frame())

        assert overlay._chooser.hide_sensitive_available is False
        overlay._chooser._on_control_hovered(overlay._chooser.row.hide_flag, True)
        assert overlay._chooser.hint.text == "Windows only for now"


class TestCopyText:
    """Copy text (#82): its own action on the floating bar, not a fourth
    destination -- see FloatingBar's own docstring and OverlayWindow.
    copy_text. Driven through a faked recognizer, the same way
    TestHideSensitiveText above is, so nothing here depends on the machine
    actually having OCR.
    """

    SELECTION = QRect(100, 50, 400, 300)

    @pytest.fixture(autouse=True)
    def _clean_slate(self):
        _close_stray_toplevel_windows()

    @pytest.fixture
    def recognizer(self, monkeypatch):
        """Replaces the platform's text recognition. `lines` is what it
        returns, each a list of (text, x, y, w, h) words already in
        reading order, the way OCR hands words back within one line."""
        state = SimpleNamespace(
            available=True,
            reason="",
            lines=[
                [("Hello,", 0, 0, 60, 20), ("world.", 70, 0, 60, 20)],
                [("Second", 0, 30, 70, 20), ("line.", 80, 30, 50, 20)],
            ],
        )
        current = overlay_module.platform.current

        def recognize_text(image):
            return [
                [RecognizedWord(text, QRectF(x, y, w, h)) for text, x, y, w, h in line]
                for line in state.lines
            ]

        monkeypatch.setattr(current, "recognizes_text", lambda: state.available)
        monkeypatch.setattr(current, "text_recognition_unavailable_reason", lambda: state.reason)
        monkeypatch.setattr(current, "recognize_text", recognize_text)
        return state

    def _overlay(self):
        frame = make_frame(image_size=(1600, 1000), logical_size=(1600, 1000))
        overlay = OverlayWindow(frame)
        overlay.set_selection(self.SELECTION)
        return overlay

    def test_lines_come_back_in_reading_order_one_per_line(self, recognizer, monkeypatch):
        copied = []
        monkeypatch.setattr(output_module, "copy_text_to_clipboard", copied.append)
        overlay = self._overlay()

        overlay.copy_text()

        assert copied == ["Hello, world.\nSecond line."]

    def test_the_toast_says_how_many_lines_came_back(self, recognizer, monkeypatch):
        monkeypatch.setattr(output_module, "copy_text_to_clipboard", lambda text: None)
        overlay = self._overlay()
        said = []
        overlay._show_toast = lambda icon, text: said.append(text)

        overlay.copy_text()

        assert said == ["Copied 2 lines"]

    def test_a_single_line_is_not_pluralised(self, recognizer, monkeypatch):
        recognizer.lines = [[("Solo", 0, 0, 40, 20)]]
        monkeypatch.setattr(output_module, "copy_text_to_clipboard", lambda text: None)
        overlay = self._overlay()
        said = []
        overlay._show_toast = lambda icon, text: said.append(text)

        overlay.copy_text()

        assert said == ["Copied 1 line"]

    def test_finding_nothing_says_so_and_leaves_the_clipboard_alone(self, recognizer, monkeypatch):
        recognizer.lines = []
        copied = []
        monkeypatch.setattr(output_module, "copy_text_to_clipboard", copied.append)
        overlay = self._overlay()
        said = []
        overlay._show_toast = lambda icon, text: said.append(text)

        overlay.copy_text()

        assert copied == []
        assert said == ["No text found"]

    def test_a_line_seen_in_both_reads_is_kept_once(self, recognizer, monkeypatch):
        # windows_ocr reads a small selection twice, at its own size and
        # doubled, and hands both reads' lines back one after another --
        # the same duplication TestHideSensitiveText's own
        # test_the_same_value_found_by_both_reads_is_one_box_covering_both
        # covers for the redaction-box side of this.
        recognizer.lines = [
            [("Hello,", 0, 0, 60, 20), ("world.", 70, 0, 60, 20)],
            [("Hello,", 0, 0, 60, 20), ("world.", 70, 0, 60, 20)],
        ]
        copied = []
        monkeypatch.setattr(output_module, "copy_text_to_clipboard", copied.append)
        overlay = self._overlay()

        overlay.copy_text()

        assert copied == ["Hello, world."]

    def test_the_recognizer_reads_the_selection_at_full_resolution(self, recognizer, monkeypatch):
        # The same crop Hide sensitive hands the engine -- not
        # rendered_image(), which flattens ink onto it (see copy_text's
        # own docstring).
        monkeypatch.setattr(output_module, "copy_text_to_clipboard", lambda text: None)
        overlay = self._overlay()
        seen = []
        monkeypatch.setattr(
            overlay_module.platform.current,
            "recognize_text",
            lambda image: seen.append(QImage(image)) or [],
        )

        overlay.copy_text()

        [image] = seen
        assert (image.width(), image.height()) == (400, 300)

    def test_the_bar_greys_copy_text_with_the_platforms_reason(self, recognizer):
        recognizer.available = False
        recognizer.reason = "Windows only for now"

        overlay = self._overlay()

        assert overlay._bar.copy_text_available is False
        assert overlay._bar._copy_text_button.unavailable_reason == "Windows only for now"

    def test_the_button_click_is_refused_while_the_platform_cannot(self, recognizer, monkeypatch):
        recognizer.available = False
        recognizer.reason = "Windows only for now"
        copied = []
        monkeypatch.setattr(output_module, "copy_text_to_clipboard", copied.append)
        overlay = self._overlay()
        said = []
        overlay._show_toast = lambda icon, text: said.append(text)

        # Not setEnabled(False) -- the tooltip explaining why must survive
        # (_CopyTextButton's own docstring) -- so the click still reaches
        # Qt; it is FloatingBar._on_copy_text_pressed that refuses it.
        overlay._bar._copy_text_button.click()

        assert copied == []
        assert said == []

    def test_the_key_is_refused_while_the_platform_cannot(self, recognizer, monkeypatch):
        recognizer.available = False
        recognizer.reason = "Windows only for now"
        copied = []
        monkeypatch.setattr(output_module, "copy_text_to_clipboard", copied.append)
        overlay = self._overlay()
        said = []
        overlay._show_toast = lambda icon, text: said.append(text)

        QTest.keyClick(overlay, Qt.Key.Key_X)

        # A greyed button refuses its own click, and the key refuses it the
        # same way -- neither the clipboard nor the toast moves at all.
        assert copied == []
        assert said == []

    def test_the_key_copies_text_and_ends_the_snip(self, recognizer, monkeypatch):
        monkeypatch.setattr(output_module, "copy_text_to_clipboard", lambda text: None)
        overlay = self._overlay()
        overlay.show()
        QTest.qWaitForWindowExposed(overlay)

        QTest.keyClick(overlay, Qt.Key.Key_X)

        assert not overlay.isVisible()

    def test_the_button_click_copies_text_and_ends_the_snip(self, recognizer, monkeypatch):
        monkeypatch.setattr(output_module, "copy_text_to_clipboard", lambda text: None)
        overlay = self._overlay()
        overlay.show()
        QTest.qWaitForWindowExposed(overlay)

        overlay._bar.copyTextRequested.emit()

        assert not overlay.isVisible()


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

    def test_instant_finishes_the_moment_the_page_is_selected(self, monkeypatch):
        # `instant` means "no window, just do it", and for this mode the
        # selection *is* the whole decision -- there is nothing left to
        # choose once the page has been found. It was briefly otherwise:
        # while full-page capture was on the chooser, a browser selection
        # had a second question after it (visible or whole), so `instant`
        # had to be held back until that was answered. With that question
        # gone, holding back would just mean a mode that never finishes.
        copied = []
        monkeypatch.setattr(output_module, "copy_image_to_clipboard", copied.append)
        monkeypatch.setattr(setup_desktop, "load_instant_saves", lambda: False)
        overlay = self._overlay(QRectF(-1720, 300, 1280, 700))
        overlay._on_captured = lambda image, path: None
        overlay._chooser.set_after("instant")

        overlay._dispatch_capture_mode(tokens.BROWSER_MODE)

        assert overlay.outcome == "instant"
        assert len(copied) == 1

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


def _snipux_on_screen() -> bool:
    return any(widget.isVisible() for widget in QApplication.topLevelWidgets())


class _FakeFocusedWindowProvider(UnsupportedGeometryProvider):
    """Answers `active_window` with each of `answers` in turn, the last
    repeating, and records whether any snipux window was on screen at each
    ask. `available` is `is_available`'s answer."""

    def __init__(self, *answers, available=True):
        self._answers = list(answers)
        self._available = available
        self.asked_while_on_screen: list[bool] = []

    def is_available(self):
        return self._available

    def active_window(self):
        self.asked_while_on_screen.append(_snipux_on_screen())
        if len(self._answers) > 1:
            return self._answers.pop(0)
        return self._answers[0] if self._answers else None


class _WhateverHasFocusProvider(UnsupportedGeometryProvider):
    """Answers the way a platform's focus query does: with whatever has
    focus when it is asked. Once any snipux window is on screen, that is
    snipux itself -- `SNIPUX`, the whole overlay -- which is exactly the
    answer the overlay must never end up taking."""

    USERS_WINDOW = QRectF(-1720, 300, 1280, 700)
    SNIPUX = QRectF(-1920, 0, 3840, 1080)

    def is_available(self):
        return True

    def active_window(self):
        if _snipux_on_screen():
            return ("snipux", self.SNIPUX)
        return ("notes", self.USERS_WINDOW)


class TestActiveWindowTakesTheWindowTheUserWasIn:
    """Active window takes the focused application's window the moment it
    is chosen, with nothing to aim at.

    Driven entirely through fake providers, like the Browser tests above:
    nothing here may pass or fail by what is focused on the machine
    running it.
    """

    @pytest.fixture(autouse=True)
    def _clean_slate(self, monkeypatch):
        _close_stray_toplevel_windows()
        # Nothing to pin: the side `set_kind("record")` picks below lasts
        # for this overlay alone and is never written anywhere.

    # A desktop whose origin is not (0, 0), so a local-for-absolute mix-up
    # is a whole monitor's error rather than an invisible one.
    LEFT = QRectF(-1920, 0, 1920, 1080)
    PRIMARY = QRectF(0, 0, 1920, 1080)
    ORIGIN = (-1920, 0)
    SIZE = (3840, 1080)
    # Absolute (-1720, 300) is window-local (200, 300) against ORIGIN.
    NOTES = ("notes", QRectF(-1720, 300, 1280, 700))
    NOTES_LOCAL = QRect(200, 300, 1280, 700)

    def _frame(self):
        return make_frame(image_size=self.SIZE, logical_size=self.SIZE, logical_origin=self.ORIGIN)

    def _overlay(self, provider, **kwargs):
        overlay = OverlayWindow(
            self._frame(),
            monitor_geometries=[self.PRIMARY, self.LEFT],
            geometry_provider=provider,
            **kwargs,
        )
        overlay.setGeometry(round(self.ORIGIN[0]), round(self.ORIGIN[1]), *self.SIZE)
        overlay.show()
        QTest.qWaitForWindowExposed(overlay)
        return overlay

    def test_choosing_it_takes_the_window_without_arming(self):
        overlay = self._overlay(_FakeFocusedWindowProvider(self.NOTES))

        overlay._chooser.set_mode(tokens.ACTIVE_WINDOW_MODE)

        assert overlay._selection == self.NOTES_LOCAL
        # Nothing to aim at, so it takes the window on the pick -- and a
        # selection is what folds the row down to its tab (#66).
        assert overlay._chooser.phase == "collapsed"

    def test_its_shortcut_takes_it_too(self):
        overlay = self._overlay(_FakeFocusedWindowProvider(self.NOTES))

        overlay._chooser.handle_key(ord("A"), "A")

        assert overlay._selection == self.NOTES_LOCAL

    def test_its_key_is_not_taken_once_something_is_selected(self):
        # A is also Arrow on the stills bar. The chooser takes keys only
        # while nothing is selected, so the two never compete for it.
        overlay = self._overlay(_FakeFocusedWindowProvider(self.NOTES))
        overlay.set_selection(QRect(10, 10, 100, 100))

        QTest.keyClick(overlay, Qt.Key.Key_A)

        assert overlay._chooser.mode != tokens.ACTIVE_WINDOW_MODE
        assert overlay._selection == QRect(10, 10, 100, 100)

    def test_snipux_is_never_the_window_it_takes(self):
        # The trap: by the time the row is picked the chooser is on screen
        # and is the focused window, so a provider asked then names snipux.
        # This one answers the way a real focus query does, and the snip
        # must still land on the window the user was in.
        overlay = self._overlay(_WhateverHasFocusProvider())
        assert _snipux_on_screen()

        overlay._chooser.set_mode(tokens.ACTIVE_WINDOW_MODE)

        assert overlay._selection == self.NOTES_LOCAL

    def test_it_is_asked_once_before_snipux_is_on_screen(self):
        provider = _FakeFocusedWindowProvider(self.NOTES)
        overlay = self._overlay(provider)

        overlay._dispatch_capture_mode(tokens.ACTIVE_WINDOW_MODE)
        overlay._dispatch_capture_mode(tokens.ACTIVE_WINDOW_MODE)

        assert provider.asked_while_on_screen == [False]

    def test_the_row_is_offered_when_a_window_is_found(self):
        overlay = self._overlay(_FakeFocusedWindowProvider(self.NOTES))

        assert overlay._chooser._unavailable_reason(tokens.ACTIVE_WINDOW_MODE) is None

    def test_the_row_is_greyed_when_there_is_nothing_to_take(self):
        overlay = self._overlay(_FakeFocusedWindowProvider(None))

        assert (
            overlay._chooser._unavailable_reason(tokens.ACTIVE_WINDOW_MODE)
            == tokens.ACTIVE_WINDOW_UNAVAILABLE
        )

    def test_the_row_says_so_where_the_platform_cannot_name_one(self):
        # UnsupportedGeometryProvider is what Wayland gets.
        overlay = self._overlay(UnsupportedGeometryProvider())

        assert (
            overlay._chooser._unavailable_reason(tokens.ACTIVE_WINDOW_MODE)
            == tokens.ACTIVE_WINDOW_UNSUPPORTED
        )

    def test_nothing_found_selects_nothing(self):
        overlay = self._overlay(_FakeFocusedWindowProvider(None))

        overlay._dispatch_capture_mode(tokens.ACTIVE_WINDOW_MODE)

        assert overlay._selection is None

    def test_a_window_across_two_monitors_comes_out_whole(self):
        overlay = self._overlay(_FakeFocusedWindowProvider(("notes", QRectF(-400, 100, 800, 600))))

        overlay._dispatch_capture_mode(tokens.ACTIVE_WINDOW_MODE)

        assert overlay._selection == QRect(1520, 100, 800, 600)

    def test_a_window_reaching_past_the_desktop_is_clipped_to_it(self):
        overlay = self._overlay(_FakeFocusedWindowProvider(("notes", QRectF(-2000, 300, 1280, 700))))

        overlay._dispatch_capture_mode(tokens.ACTIVE_WINDOW_MODE)

        assert overlay._selection == QRect(0, 300, 1200, 700)

    def test_a_window_entirely_off_the_desktop_selects_nothing(self):
        overlay = self._overlay(_FakeFocusedWindowProvider(("notes", QRectF(9000, 9000, 800, 600))))

        overlay._dispatch_capture_mode(tokens.ACTIVE_WINDOW_MODE)

        assert overlay._selection is None

    def test_instant_finishes_the_moment_it_is_chosen(self, monkeypatch):
        copied = []
        monkeypatch.setattr(output_module, "copy_image_to_clipboard", copied.append)
        monkeypatch.setattr(setup_desktop, "load_instant_saves", lambda: False)
        overlay = self._overlay(_FakeFocusedWindowProvider(self.NOTES))
        overlay._on_captured = lambda image, path: None
        overlay._chooser.set_after("instant")

        overlay._chooser.set_mode(tokens.ACTIVE_WINDOW_MODE)

        assert overlay.outcome == "instant"
        assert len(copied) == 1

    def test_on_the_record_side_it_frames_the_window_for_recording(self):
        requests = []
        overlay = self._overlay(
            _FakeFocusedWindowProvider(self.NOTES),
            on_recording_requested=lambda rect, delay, after: requests.append(rect),
        )
        overlay._chooser.set_kind("record")

        overlay._chooser.set_mode(tokens.ACTIVE_WINDOW_MODE)

        assert requests == [self.NOTES[1]]
        assert overlay._armed_for_recording is True

    def _choose_after_a_delay(self, provider):
        registry = BackendRegistry([_FakeCaptureBackend(self._frame())])
        overlay = self._overlay(provider, registry=registry)
        overlay._on_delay_changed(tokens.DELAYS[1])
        overlay._chooser.set_mode(tokens.ACTIVE_WINDOW_MODE)
        assert not overlay.isVisible()
        for _ in range(3):
            overlay._delay_timer.timeout.emit()
        return overlay

    def test_after_a_delay_it_takes_the_window_focused_by_then(self):
        # A delay exists so the screen can change first, and which window
        # has focus is part of that.
        terminal = ("terminal", QRectF(100, 50, 900, 500))
        provider = _FakeFocusedWindowProvider(self.NOTES, terminal)

        overlay = self._choose_after_a_delay(provider)

        assert overlay._selection == QRect(2020, 50, 900, 500)
        assert provider.asked_while_on_screen == [False, False]

    def test_after_a_delay_it_keeps_the_first_window_if_focus_has_not_settled(self):
        provider = _FakeFocusedWindowProvider(self.NOTES, None)

        overlay = self._choose_after_a_delay(provider)

        assert overlay._selection == self.NOTES_LOCAL

    def test_a_provider_that_knows_nothing_about_focus_still_works(self):
        assert UnsupportedGeometryProvider().active_window() is None


class TestReuseLastRegionPreselectsIt:
    """Last region: the rectangle the last snip came from, as a mode (#66).

    It was a toggle on the row meaning "open on the last region". It is a
    row of the mode menu now, and choosing it both takes that rectangle and
    is what the next snip opens on: the same stored `reuse_last_region`
    preference, written by the pick where the toggle used to write it.
    Opening on it still only offers the rectangle, committing nothing.

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

    # -- the mode --------------------------------------------------------

    @staticmethod
    def _pick(overlay, mode):
        QTest.mouseClick(overlay._chooser.row.mode_chip, Qt.MouseButton.LeftButton)
        QTest.mouseClick(overlay._chooser._menu._rows[mode], Qt.MouseButton.LeftButton)

    def test_its_row_offers_the_dimensions_it_restores(self):
        setup_desktop.save_last_region((-1720, 300, 640, 480))

        _rows, spec = self._overlay()._chooser._mode_rows()

        assert (spec.disabled, spec.subtitle) == (False, "640 × 480")

    def test_with_nothing_captured_yet_it_is_greyed_with_the_reason(self):
        _rows, spec = self._overlay()._chooser._mode_rows()

        assert (spec.disabled, spec.subtitle) == (True, tokens.LAST_REGION_NONE)

    def test_a_rectangle_off_these_monitors_greys_it_with_that_reason(self):
        setup_desktop.save_last_region((-1720, 300, 640, 480))

        overlay = self._overlay(
            monitors=[QRectF(self.PRIMARY)], origin=(0, 0), size=(1920, 1080)
        )

        _rows, spec = overlay._chooser._mode_rows()
        assert (spec.disabled, spec.subtitle) == (True, tokens.LAST_REGION_OFF_DESK)

    def test_choosing_it_frames_the_previous_capture(self):
        setup_desktop.save_last_region((-1720, 300, 640, 480))
        overlay = self._overlay()

        self._pick(overlay, tokens.LAST_REGION_MODE)

        # Absolute (-1720, 300) is window-local (200, 300) here.
        assert overlay._selection == QRect(200, 300, 640, 480)
        # Chosen, so taken -- not the offer the overlay makes as it opens.
        assert overlay._recalled_selection is False
        assert overlay._chooser.phase == "collapsed"

    def test_shift_r_frames_it_too(self):
        setup_desktop.save_last_region((-1720, 300, 640, 480))
        overlay = self._overlay()

        QTest.keyClick(overlay, Qt.Key.Key_R, Qt.KeyboardModifier.ShiftModifier)

        assert overlay._selection == QRect(200, 300, 640, 480)

    def test_choosing_it_with_instant_finishes_on_it(self, monkeypatch):
        copied = []
        monkeypatch.setattr(output_module, "copy_image_to_clipboard", copied.append)
        monkeypatch.setattr(setup_desktop, "load_instant_saves", lambda: False)
        setup_desktop.save_last_region((-1720, 300, 640, 480))
        overlay = self._overlay()
        overlay._chooser.set_after("instant")

        self._pick(overlay, tokens.LAST_REGION_MODE)

        assert len(copied) == 1
        assert not overlay.isVisible()

    def test_choosing_it_is_what_the_next_snip_opens_on(self):
        setup_desktop.save_last_region((-1720, 300, 640, 480))
        self._pick(self._overlay(), tokens.LAST_REGION_MODE)

        assert setup_desktop.load_reuse_last_region() is True
        second = self._overlay()
        assert second._chooser.mode == tokens.LAST_REGION_MODE
        assert second._selection == QRect(200, 300, 640, 480)

    def test_choosing_another_mode_is_what_turns_that_off(self):
        setup_desktop.save_last_region((-1720, 300, 640, 480))
        setup_desktop.save_reuse_last_region(True)
        overlay = self._overlay()

        # Opened on the region, so the row is a tab: Space brings it back.
        QTest.keyClick(overlay, Qt.Key.Key_Space)
        self._pick(overlay, "Region")

        assert setup_desktop.load_reuse_last_region() is False

    def test_the_stored_preference_is_still_what_it_opens_on(self):
        # Nobody who had the toggle on loses their rectangle to the change.
        setup_desktop.save_last_region((-1720, 300, 640, 480))
        setup_desktop.save_reuse_last_region(True)

        overlay = self._overlay()

        assert overlay._chooser.mode == tokens.LAST_REGION_MODE
        assert overlay._chooser.reuse_last_region is True

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
        # Opening on it is the offer the toggle used to make: nothing is
        # picked and nothing is clicked.
        setup_desktop.save_last_region((-1720, 300, 640, 480))
        setup_desktop.save_reuse_last_region(True)

        overlay = self._overlay()

        assert overlay._chooser.mode == tokens.LAST_REGION_MODE
        assert overlay._recalled_selection is True
        # Under the rule rather than among the capture modes, which the
        # bar's own mode popover lists.
        assert tokens.LAST_REGION_MODE not in [m[0] for m in tokens.CAPTURE_MODES]

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

    def test_a_recalled_region_arms_no_recording_when_the_side_is_flipped(self):
        # Committing is what arms a recording, and arming one as a side
        # effect of opening the overlay -- or of reaching for the record
        # side afterwards -- is not something anyone asked for. Every
        # overlay opens on stills now (bars/divergences.md 25), so this is
        # the only way the recalled rectangle and the record side meet.
        setup_desktop.save_last_region((-1720, 300, 640, 480))
        setup_desktop.save_reuse_last_region(True)
        overlay = self._overlay()

        overlay._chooser.set_kind("record")

        assert overlay._selection == QRect(200, 300, 640, 480)
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
        monkeypatch.setattr(output_module, "copy_image_to_clipboard", lambda image: None)
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
        return QRectF(overlay._chooser.row.geometry())

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

    def test_before_any_move_it_is_the_monitor_the_os_has_the_pointer_on(
        self, monkeypatch
    ):
        _point_the_os_at(monkeypatch, STAGGERED_RIGHT)

        assert self._overlay()._active_screen_rect() == STAGGERED_RIGHT

    def test_a_pointer_on_no_monitor_this_window_covers_gets_one_it_does(
        self, monkeypatch
    ):
        # A `QScreen` lookup answered with that screen's own geometry even
        # when this window does not cover it -- on Wayland, where it covers
        # only the interactive monitor -- so the row was placed off the
        # window. Here the pointer is in the gap above the left monitor;
        # the offscreen primary is not one of these monitors, so the first
        # listed is the answer.
        _point_the_os_at(monkeypatch, QRectF(290, 10, 20, 20))

        assert self._overlay()._active_screen_rect() == STAGGERED_CENTRE


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
        expected_top = QRectF(QRect(2600, 500, 700, 400)).bottom() + tokens.BarMetric.BAR_OFFSET_Y
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

    def test_the_style_popover_stays_on_the_selections_monitor(self):
        overlay = self._overlay(QRect(300, 950, 600, 250))
        overlay._bar.select_tool("arrow")
        overlay._toggle_style()

        popover = overlay._style_popover.geometry()

        assert overlay._style_popover.isVisible()
        assert self._on_a_monitor(popover), f"popover at {popover} is on no monitor"

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

    def test_a_drag_that_lands_next_door_takes_its_toolbar_with_it(self):
        # Reported: a region drawn over the whole of the next monitor, begun
        # a few pixels the wrong side of the bezel, kept its toolbar on the
        # monitor the press landed on -- a screen away from every pixel
        # being marked up. The monitor the drag started on holds the chrome
        # while a fair share of the selection is still on it, and no longer.
        overlay = self._empty_overlay()

        self._drag(overlay, QPoint(1900, 200), QPoint(3800, 1300))

        bar = overlay._bar.geometry()
        assert QRectF(STAGGERED_CENTRE).contains(QRectF(bar)), f"bar at {bar}"

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
        overlay._bar.select_tool("arrow")

        self._drag(overlay, QPoint(200, 200), QPoint(300, 300))

        assert overlay._selection == before, "a stroke must not restart the selection"
        assert overlay._marks, "the stroke should have been committed"

    def test_marks_survive_starting_a_new_selection(self):
        # Marks are in window coordinates, so a new selection must not
        # destroy them -- Ctrl+Z could not bring them back.
        overlay = self._overlay()
        self._drag(overlay, QPoint(100, 100), QPoint(500, 400))
        overlay._bar.select_tool("arrow")
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
        overlay._bar.select_tool("arrow")
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

    def _open_family_menu(self, overlay, family: str) -> FamilyMenu:
        QTest.mouseClick(overlay._bar._tool_buttons[family].notch, Qt.MouseButton.LeftButton)
        menu = overlay._family_menus[family]
        assert menu.isVisible()
        return menu

    def test_a_press_on_a_family_menu_row_reaches_the_row_not_the_frame(self):
        # The one that bites: a press the frame's handler saw first would
        # close the menu before the row's click landed, so every row would
        # look dead. The menu opens over the selection with the pen armed,
        # so a leaked press would start a stroke as well.
        overlay = self._overlay()
        before = self._selected(overlay)
        overlay._bar.select_tool("pen")
        menu = self._open_family_menu(overlay, "shapes")
        row = menu._rows["ellipse"]
        centre = row.rect().center()
        assert QRectF(before).contains(QPointF(row.mapTo(overlay, centre)))

        QTest.mousePress(row, Qt.MouseButton.LeftButton, pos=centre)

        assert menu.isVisible()
        assert overlay._in_progress_shape is None
        assert overlay._selection == before

        QTest.mouseRelease(row, Qt.MouseButton.LeftButton, pos=centre)

        assert overlay._bar.active_tool == "ellipse"
        assert not menu.isVisible()
        assert overlay.marks == ()

    def test_a_press_on_a_family_menus_own_background_keeps_it_open(self):
        overlay = self._overlay()
        before = self._selected(overlay)
        overlay._bar.select_tool("pen")
        menu = self._open_family_menu(overlay, "redact")

        self._press(menu, self._background_of(menu))

        assert menu.isVisible()
        assert overlay._selection == before
        assert overlay._in_progress_shape is None

    def test_a_press_on_the_open_style_popover_keeps_it_open(self):
        overlay = self._overlay()
        before = self._selected(overlay)
        overlay._bar.select_tool("pen")
        QTest.mouseClick(overlay._bar._style_dot, Qt.MouseButton.LeftButton)
        popover = overlay._style_popover
        assert popover.isVisible()

        self._press(popover, self._background_of(popover))

        assert popover.isVisible()
        assert overlay._selection == before
        assert overlay._in_progress_shape is None

    def test_a_press_on_a_swatch_reaches_the_swatch_not_the_frame(self):
        # The popover opens over the selection with the pen armed, so a press
        # the frame saw first would close it and start a stroke.
        overlay = self._overlay()
        before = self._selected(overlay)
        overlay._bar.select_tool("pen")
        QTest.mouseClick(overlay._bar._style_dot, Qt.MouseButton.LeftButton)
        _name, violet = tokens.INK_SWATCHES[4]
        swatch = overlay._style_popover._swatch_buttons[violet]
        centre = swatch.rect().center()

        QTest.mousePress(swatch, Qt.MouseButton.LeftButton, pos=centre)
        QTest.mouseRelease(swatch, Qt.MouseButton.LeftButton, pos=centre)

        assert overlay._style_popover.isVisible()
        assert overlay._styles.of("pen").colour == violet
        assert overlay._in_progress_shape is None
        assert overlay._selection == before
        assert overlay.marks == ()

    def test_a_press_on_the_frame_while_a_menu_is_open_only_closes_it(self):
        overlay = self._overlay()
        before = self._selected(overlay)
        overlay._bar.select_tool("pen")
        menu = self._open_family_menu(overlay, "shapes")

        QTest.mousePress(overlay, Qt.MouseButton.LeftButton, pos=QPoint(300, 200))
        QTest.mouseRelease(overlay, Qt.MouseButton.LeftButton, pos=QPoint(300, 200))

        assert not menu.isVisible()
        assert overlay._selection == before
        assert overlay.marks == ()

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

    def test_where_the_platform_skips_the_animation_it_maps_at_full_opacity(self, monkeypatch):
        # Nothing to wait out, so nothing to hide: the reveal wait was nearly
        # half of the time a snip took to appear.
        monkeypatch.setattr(
            overlay_module.platform.current, "skip_map_animation", lambda widget: True
        )
        overlay = self._overlay()

        overlay.show_on_screen(None)

        assert overlay.windowOpacity() == 1.0

    def test_the_platform_is_asked_before_the_window_exists(self, monkeypatch):
        # A window type is read when the native window is created, so asking
        # any later would change nothing.
        created = []
        monkeypatch.setattr(
            overlay_module.platform.current,
            "skip_map_animation",
            lambda widget: created.append(widget.testAttribute(Qt.WidgetAttribute.WA_WState_Created))
            or False,
        )

        self._overlay()

        assert created == [False]

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
        # Clamped to the screen this process actually has. `_reserved_margins`
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

    def test_it_starts_in_choosing_with_the_row_up(self):
        overlay = self._overlay()

        assert overlay._chooser.phase == "choosing"
        assert overlay._chooser.row.isVisibleTo(overlay)

    def test_picking_a_mode_leaves_the_row_up(self):
        # #66: picking a mode does not arm it. The row stays until the user
        # drags or clicks a window, and Window's preview works beneath it.
        overlay = self._overlay()

        overlay._chooser.set_mode("Window")

        assert overlay._chooser.phase == "choosing"
        assert overlay._chooser.row.isVisibleTo(overlay)
        assert not overlay._chooser.tab.isVisibleTo(overlay)

    def test_the_armed_mode_reaches_the_overlay(self):
        overlay = self._overlay()

        overlay._chooser.set_mode("Window")

        assert overlay._capture_mode == "Window"

    def test_full_screen_fires_instead_of_arming(self):
        # It has nothing left to aim at, so choosing it *is* the capture --
        # tokens.IMMEDIATE_MODES carries that rather than a name check.
        overlay = self._overlay()
        fired = []
        overlay._chooser.fireImmediately.connect(fired.append)

        overlay._chooser.set_mode("Full screen")

        assert fired == ["Full screen"]
        # It takes the monitor on the pick, and a selection folds the row.
        assert overlay._chooser.phase == "collapsed"

    def test_the_tab_reopens_the_row_with_selections_intact(self):
        overlay = self._overlay()
        overlay._chooser.set_after("review")
        overlay._chooser.set_mode("Window", announce=False)
        overlay.set_selection(QRect(100, 200, 300, 200))
        assert overlay._chooser.phase == "collapsed"

        overlay._chooser.reopen()

        assert overlay._chooser.phase == "choosing"
        assert overlay._chooser.row.isVisibleTo(overlay)
        assert overlay._chooser.mode == "Window"
        assert overlay._chooser.after == "review"

    @pytest.mark.parametrize("key,mode", list(tokens.MODE_KEYS.items()))
    def test_each_shortcut_selects_its_mode(self, key, mode):
        overlay = self._overlay()
        # `Tab` is greyed, and its key correspondingly inert, without a
        # browser -- seeded so this test stays about the key map rather
        # than that rule, which test_chooser.py covers on its own. Active
        # window likewise.
        overlay._chooser.set_browser_available(True)
        overlay._chooser.set_active_window_available(True)
        fired = []
        overlay._chooser.fireImmediately.connect(fired.append)

        overlay._chooser.handle_key(ord(key), key)

        assert overlay._chooser.mode == mode

    def test_space_reopens_from_the_tab(self):
        overlay = self._overlay()
        overlay.set_selection(QRect(100, 200, 300, 200))
        assert overlay._chooser.phase == "collapsed"

        QTest.keyClick(overlay, Qt.Key.Key_Space)

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

    def test_it_folds_to_its_tab_once_there_is_a_selection(self):
        # #66: the tab stays up beside the bar, carrying the mode, the
        # destination and Hide sensitive through the change of stage.
        overlay = self._overlay()

        overlay.set_selection(QRect(100, 100, 300, 200))

        assert not overlay._chooser.row.isVisibleTo(overlay)
        assert overlay._chooser.tab.isVisibleTo(overlay)
        assert overlay._bar.isVisibleTo(overlay)

    def test_the_bars_chip_is_seeded_from_it(self):
        # One piece of state, two surfaces -- do not duplicate it.
        overlay = self._overlay()

        overlay._chooser.set_mode("Window")

        assert overlay._bar._chip._text_label.text() == "Window"

    def test_seeding_back_from_the_bar_does_not_rearm(self):
        # Arming on the way back would re-emit into the handler that sent
        # it, which is an infinite loop rather than a design.
        overlay = self._overlay()
        overlay._chooser.reopen()

        overlay._chooser.set_mode("Window", announce=False)

        assert overlay._chooser.mode == "Window"
        assert overlay._chooser.phase == "choosing"

    def test_the_row_hangs_from_the_active_monitors_top_edge(self):
        # Never the virtual desktop: on a staggered multi-monitor setup its
        # centre is a gap between screens.
        overlay = self._overlay()

        row = overlay._chooser.row.geometry()
        screen = overlay._active_screen_rect()
        assert row.top() == round(screen.y() - overlay.geometry().top())
        assert abs(row.center().x() - (screen.center().x() - overlay.geometry().left())) <= 2

    def test_the_row_clears_whatever_the_desktop_reserves_up_there(
        self, monkeypatch
    ):
        # GNOME paints its top bar over an always-on-top window, so a row
        # hung flush against that edge is behind it -- 32px of the 42px row
        # on the measured desktop, and all 22px of the tab.
        monkeypatch.setattr(
            overlay_module.platform.current,
            "reserved_margins",
            lambda screen: QMargins(0, 32, 0, 0),
        )
        overlay = self._overlay()

        screen = overlay._active_screen_rect()
        top = round(screen.y() - overlay.geometry().top())
        assert overlay._chooser.row.geometry().top() == top + 32

    def test_the_tab_clears_it_too(self, monkeypatch):
        monkeypatch.setattr(
            overlay_module.platform.current,
            "reserved_margins",
            lambda screen: QMargins(0, 32, 0, 0),
        )
        overlay = self._overlay()

        overlay.set_selection(QRect(100, 200, 300, 200))

        screen = overlay._active_screen_rect()
        top = round(screen.y() - overlay.geometry().top())
        assert overlay._chooser.tab.geometry().top() == top + 32

    def test_the_close_button_clears_it_as_well(self, monkeypatch):
        # Same corner, same bar: a close button under it cannot be clicked,
        # and it is the only visible way to cancel a snip.
        overlay = self._overlay()
        flush = overlay._close_button.geometry().top()
        monkeypatch.setattr(
            overlay_module.platform.current,
            "reserved_margins",
            lambda screen: QMargins(0, 32, 0, 0),
        )
        overlay._reserved_margins_cache.clear()

        overlay._reposition_close_button()

        assert overlay._close_button.geometry().top() == flush + 32

    def test_the_delay_defaults_to_none_and_is_selectable(self):
        overlay = self._overlay()
        assert overlay._chooser.delay == tokens.DELAY_DEFAULT

        overlay._chooser.set_delay("5s")

        assert overlay._chooser.delay == "5s"


class TestChromeClearsTheDesktopsOwnDock:
    """A dock or taskbar paints over this window, so every piece of floating
    chrome is clamped inside its monitor minus what the desktop reserves.

    Reported with the Ubuntu Dock fixed to the bottom: a selection reaching
    the bottom of the monitor put the bar and its tool hint under the dock,
    and none of the controls could be clicked.
    """

    DOCK = QMargins(0, 32, 0, 71)

    def _overlay(self, monkeypatch, margins=DOCK):
        monkeypatch.setattr(
            overlay_module.platform.current, "reserved_margins", lambda screen: margins
        )
        # The whole offscreen screen, so `screenAt` finds it and the
        # platform is actually asked, at any scale factor.
        available = QGuiApplication.primaryScreen().geometry()
        size = (available.width(), available.height())
        frame = make_frame(image_size=size, logical_size=size)
        overlay = OverlayWindow(frame)
        overlay.setGeometry(0, 0, *size)
        overlay.show()
        QTest.qWaitForWindowExposed(overlay)
        return overlay

    def test_a_whole_monitor_selection_keeps_the_bar_above_a_bottom_dock(
        self, monkeypatch
    ):
        overlay = self._overlay(monkeypatch)

        overlay.set_selection(overlay.rect())

        assert overlay._bar.isVisible()
        assert overlay._bar.geometry().bottom() <= overlay.rect().bottom() - self.DOCK.bottom()

    def test_the_tool_hint_stays_above_it_too(self, monkeypatch):
        overlay = self._overlay(monkeypatch)
        overlay.set_selection(overlay.rect())

        overlay._preview_tool("pen")

        assert overlay._tool_hint.isVisible()
        hint = overlay._tool_hint.geometry()
        assert hint.bottom() <= overlay.rect().bottom() - self.DOCK.bottom()

    def test_every_edge_the_desktop_reserves_is_left_out(self, monkeypatch):
        overlay = self._overlay(monkeypatch, QMargins(64, 32, 0, 71))

        assert overlay._chrome_bounds() == QRectF(overlay.rect()).marginsRemoved(
            QMarginsF(64, 32, 0, 71)
        )


# ---------------------------------------------------------------------------
# A monitor mounted above the others, offset (#49)
# ---------------------------------------------------------------------------
# Read off the reporting machine: three 2560x1440 monitors, two side by side
# and a third mounted above them 1164px in from the left. The frame's origin
# is that third monitor's top edge, so every window-local y is 1440 more than
# its absolute one, and a local-for-absolute slip is a whole monitor's error.
#
# The union has gaps -- x 0..1164 and 3724..5120 above y=0 are on no monitor
# -- and its centre, absolute (2560, 0), is the one point all three monitors
# touch.

ABOVE_MAIN = QRectF(0, 0, 2560, 1440)
ABOVE_SECOND = QRectF(2560, 0, 2560, 1440)
ABOVE_TOP = QRectF(1164, -1440, 2560, 1440)
MOUNTED_ABOVE = [ABOVE_MAIN, ABOVE_SECOND, ABOVE_TOP]
MOUNTED_ABOVE_ORIGIN = (0, -1440)
MOUNTED_ABOVE_SIZE = (5120, 2880)


def _point_the_os_at(monkeypatch, monitor: QRectF) -> None:
    """Put the pointer over `monitor` as far as the OS is concerned, without
    a move event ever reaching the overlay.

    That is the moment right after the shortcut: the OS knows where the
    pointer is and the overlay has not yet seen it move. Stubbed rather than
    driven because `QTest.mouseMove` moves the real global cursor, which every
    later test in the process would then inherit.
    """
    monkeypatch.setattr(
        overlay_module,
        "QCursor",
        SimpleNamespace(pos=lambda: monitor.center().toPoint()),
    )


class TestControlsLandOnTheCapturesMonitor:
    """#49: "Capturing on the second monitor puts the toolbar on the main
    monitor", on the desk above.

    Each capture mode, taken on the second monitor and on the one mounted
    above, must capture there and put every piece of chrome there too -- the
    bar, the draw tray, both popovers, the toast and the close button.

    That holds where a capture leaves the bar no room beside it, as Full
    screen does, too: the bar stays on the capture's monitor, against its
    bottom margin (#79). Only a drag takes it and what hangs off it to
    another monitor, and what belongs to the capture rather than the bar --
    the close button, the toast and the chooser's tab -- stays behind then.
    """

    @pytest.fixture(autouse=True)
    def _clean_slate(self):
        _close_stray_toplevel_windows()

    ORIGIN = QPointF(*MOUNTED_ABOVE_ORIGIN)
    ON_EACH_MONITOR = pytest.mark.parametrize(
        "monitor", [ABOVE_SECOND, ABOVE_TOP], ids=["second", "mounted-above"]
    )

    def _overlay(self, monkeypatch, pointer_on: QRectF, provider=None) -> OverlayWindow:
        _point_the_os_at(monkeypatch, pointer_on)
        frame = make_frame(
            image_size=MOUNTED_ABOVE_SIZE,
            logical_size=MOUNTED_ABOVE_SIZE,
            logical_origin=MOUNTED_ABOVE_ORIGIN,
        )
        overlay = OverlayWindow(
            frame, monitor_geometries=list(MOUNTED_ABOVE), geometry_provider=provider
        )
        # Keeps the snip open once something is committed, so the bar is
        # still there to measure.
        overlay._chooser.set_after("edit")
        overlay.show()
        QTest.qWaitForWindowExposed(overlay)
        return overlay

    def _local(self, absolute: QPointF) -> QPoint:
        return (absolute - self.ORIGIN).toPoint()

    def _chrome_on_screen(self, overlay) -> list[tuple[str, QRectF]]:
        """Every visible child widget of `overlay`, in absolute coordinates.

        Not `Chooser` itself: it is the state machine behind the row, a
        widget that draws nothing and takes no clicks, and it never moves
        from the window's corner. Its row, hint pill and tab are each a
        child here in their own right -- and the tab is up once there is a
        selection (#66), so it has to land on the capture's monitor too.
        """
        return [
            (type(child).__name__, QRectF(child.geometry()).translated(self.ORIGIN))
            for child in overlay.findChildren(
                QWidget, options=Qt.FindChildOption.FindDirectChildrenOnly
            )
            if child.isVisible() and not child.isWindow() and child is not overlay._chooser
        ]

    # What belongs to the capture rather than to the bar, and so stays on
    # the capture's monitor wherever the bar goes (#50). The tab is the
    # chooser row folded once there is a selection (#66): it names the
    # capture's mode and destination and reopens the row over the capture.
    _THE_CAPTURES_OWN = {"Toast", "_CloseButton", "_Tab"}

    def _off(self, overlay, monitor: QRectF, bar_on: QRectF | None = None) -> list[str]:
        """Every visible piece of chrome that is not where it belongs: the
        capture's own on `monitor`, and the bar and what hangs off it on
        `bar_on` (`monitor` too, unless given).
        """
        bar_on = monitor if bar_on is None else bar_on
        return [
            f"{name} at {rect}"
            for name, rect in self._chrome_on_screen(overlay)
            if not (monitor if name in self._THE_CAPTURES_OWN else bar_on).contains(rect)
        ]

    def _assert_capture_and_chrome_on(
        self, overlay, monitor: QRectF, bar_on: QRectF | None = None
    ) -> None:
        capture = overlay.absolute_selection()
        assert capture is not None, "nothing was captured"
        assert monitor.contains(capture), f"captured {capture}, not on {monitor}"
        # Each of these is placed by its own code path.
        overlay._bar.select_tool("pen")
        overlay._toggle_style()
        overlay._toggle_capture_popover()
        overlay._show_toast("save", "Saved")
        names = [name for name, _rect in self._chrome_on_screen(overlay)]
        assert {"FloatingBar", "StylePopover", "Toast", "_CloseButton"} <= set(names), names
        assert self._off(overlay, monitor, bar_on) == []
        # A family menu is placed by a path of its own too, and closes the
        # style popover as it opens -- one menu at a time -- so it is
        # checked apart.
        overlay._toggle_family_menu("shapes")
        names = [name for name, _rect in self._chrome_on_screen(overlay)]
        assert "FamilyMenu" in names, names
        assert self._off(overlay, monitor, bar_on) == []

    def _take_full_screen(self, overlay, monitor: QRectF) -> None:
        # Armed rather than taken on this desk (#53): the preview opens on
        # the monitor the pointer is on, and a click there takes it.
        overlay._chooser.set_mode("Full screen")
        assert overlay._hovered_monitor == monitor
        QTest.mouseClick(
            overlay, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
            self._local(monitor.center()),
        )

    @ON_EACH_MONITOR
    def test_a_drag(self, monkeypatch, monitor):
        overlay = self._overlay(monkeypatch, monitor)
        start = self._local(monitor.topLeft() + QPointF(700, 400))
        end = self._local(monitor.topLeft() + QPointF(1900, 1000))

        QTest.mousePress(overlay, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, start)
        QTest.mouseMove(overlay, end)
        QTest.mouseRelease(overlay, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, end)

        self._assert_capture_and_chrome_on(overlay, monitor)

    @ON_EACH_MONITOR
    def test_a_window(self, monkeypatch, monitor):
        window = QRectF(monitor.x() + 400, monitor.y() + 300, 1400, 800)
        overlay = self._overlay(monkeypatch, monitor, _FakeWindowProvider(window))

        overlay._chooser.set_mode("Window")
        QTest.mouseClick(
            overlay, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
            self._local(window.center()),
        )

        self._assert_capture_and_chrome_on(overlay, monitor)

    @ON_EACH_MONITOR
    def test_full_screen(self, monkeypatch, monitor):
        # The report's own words: the capture is on the second monitor. The
        # pointer is there and nothing has moved over the overlay yet --
        # Full screen chosen the moment it opens.
        overlay = self._overlay(monkeypatch, monitor)

        self._take_full_screen(overlay, monitor)

        assert overlay.absolute_selection() == monitor
        # A whole monitor leaves the bar no room beside it, and the bar stays
        # on that monitor all the same (#79).
        self._assert_capture_and_chrome_on(overlay, monitor)

    @ON_EACH_MONITOR
    def test_a_browser_page(self, monkeypatch, monitor):
        page = QRectF(monitor.x() + 200, monitor.y() + 150, 2000, 1100)
        overlay = self._overlay(monkeypatch, monitor, _FakeBrowserProvider(page))

        overlay._chooser.set_mode(tokens.BROWSER_MODE)

        self._assert_capture_and_chrome_on(overlay, monitor)

    @ON_EACH_MONITOR
    def test_the_active_window(self, monkeypatch, monitor):
        # Not in the issue's list, which predates the mode; the same
        # no-anchor path as Browser, and "every capture mode" covers it.
        window = QRectF(monitor.x() + 300, monitor.y() + 200, 1600, 900)
        overlay = self._overlay(
            monkeypatch, monitor, _FakeFocusedWindowProvider(("editor", window))
        )

        overlay._chooser.set_mode(tokens.ACTIVE_WINDOW_MODE)

        self._assert_capture_and_chrome_on(overlay, monitor)

    @ON_EACH_MONITOR
    def test_a_recalled_last_region(self, monkeypatch, monitor):
        region = QRectF(monitor.x() + 500, monitor.y() + 250, 1200, 700)
        setup_desktop.save_last_region(
            (round(region.x()), round(region.y()), round(region.width()), round(region.height()))
        )
        setup_desktop.save_reuse_last_region(True)

        overlay = self._overlay(monkeypatch, monitor)

        self._assert_capture_and_chrome_on(overlay, monitor)

    @ON_EACH_MONITOR
    def test_before_anything_is_selected_every_control_is_on_the_pointers_monitor(
        self, monkeypatch, monitor
    ):
        overlay = self._overlay(monkeypatch, monitor)

        names = [name for name, _rect in self._chrome_on_screen(overlay)]
        assert {"ChooserRow", "_HintPill", "_CloseButton"} <= set(names), names
        assert self._off(overlay, monitor) == []

    @ON_EACH_MONITOR
    def test_the_controls_cross_a_bezel_together(self, monkeypatch, monitor):
        # The chooser row already followed the pointer; the close button
        # stayed wherever the window's centre put it.
        overlay = self._overlay(monkeypatch, ABOVE_MAIN)

        overlay._cursor_pos = QPointF(self._local(monitor.center()))
        overlay._follow_pointer_to_its_monitor()

        assert self._off(overlay, monitor) == []

    @ON_EACH_MONITOR
    def test_a_delay_counts_down_on_the_pointers_monitor(self, monkeypatch, monitor):
        overlay = self._overlay(monkeypatch, monitor)
        overlay._delay = tokens.DELAYS[1]

        overlay._chooser.set_mode("Region")
        try:
            countdown = QRectF(overlay._countdown.geometry())
            assert monitor.contains(countdown), f"countdown at {countdown}"
        finally:
            overlay._delay_timer.stop()
            overlay._countdown.close()


class TestTheBarWithNoRoomBesideTheSelection:
    """#50 and #79 on #49's desk: three monitors, one mounted above the other
    two and offset, so the frame's origin is negative and every window-local
    y is 1440 more than its absolute one.

    A selection that leaves the bar no room beside it keeps the bar on its own
    monitor, against that monitor's bottom margin (#79). A drag can carry the
    bar, and everything hung off it, to the nearest other monitor -- inside
    that monitor's usable area, clear of its dock -- and a drag in the no-room
    case is remembered and wins next time. Room beside the selection still
    wins over all of it.
    """

    ORIGIN = QPointF(*MOUNTED_ABOVE_ORIGIN)
    MARGIN = tokens.BarMetric.BAR_EDGE_MARGIN
    OFFSET = tokens.BarMetric.BAR_OFFSET_Y
    OWN = setup_desktop.BAR_ON_OWN_MONITOR
    OTHER = setup_desktop.BAR_ON_OTHER_MONITOR
    LEFT = Qt.MouseButton.LeftButton
    # A top bar, and a dock down the left edge and along the bottom: the two
    # edges of the second monitor a bar sent over from the main one lands
    # against.
    DOCK = QMargins(64, 32, 0, 71)

    @pytest.fixture(autouse=True)
    def _clean_slate(self):
        _close_stray_toplevel_windows()

    def _overlay(
        self, monkeypatch, pointer_on: QRectF = ABOVE_MAIN, docks=None
    ) -> OverlayWindow:
        """The three-monitor overlay, with the pointer on `pointer_on` and
        `docks` -- pairs of an absolute monitor rect and the margins its
        desktop reserves -- defaulting to `DOCK` on the second monitor.
        """
        _point_the_os_at(monkeypatch, pointer_on)
        docks = [(ABOVE_SECOND, self.DOCK)] if docks is None else docks
        # Stubbed at the overlay's own seam rather than the platform's,
        # which is asked about a QScreen: none of these monitors is one.
        monkeypatch.setattr(
            OverlayWindow,
            "_reserved_margins",
            lambda _self, monitor: next(
                (margins for rect, margins in docks if rect == monitor), QMargins()
            ),
        )
        frame = make_frame(
            image_size=MOUNTED_ABOVE_SIZE,
            logical_size=MOUNTED_ABOVE_SIZE,
            logical_origin=MOUNTED_ABOVE_ORIGIN,
        )
        overlay = OverlayWindow(frame, monitor_geometries=list(MOUNTED_ABOVE))
        overlay._chooser.set_after("edit")
        overlay.show()
        QTest.qWaitForWindowExposed(overlay)
        return overlay

    def _single(self, monkeypatch) -> OverlayWindow:
        """The overlay as Wayland opens it on this desk: over the monitor
        mounted above and no other, its frame cropped to that monitor.
        """
        _point_the_os_at(monkeypatch, ABOVE_TOP)
        monkeypatch.setattr(
            OverlayWindow, "_reserved_margins", lambda _self, monitor: QMargins()
        )
        size = (round(ABOVE_TOP.width()), round(ABOVE_TOP.height()))
        frame = make_frame(
            image_size=size,
            logical_size=size,
            logical_origin=(ABOVE_TOP.x(), ABOVE_TOP.y()),
        )
        overlay = OverlayWindow(frame, monitor_geometries=[ABOVE_TOP])
        overlay.show()
        QTest.qWaitForWindowExposed(overlay)
        overlay.set_selection(QRect(0, 0, *size))
        assert overlay._bar.isVisible()
        return overlay

    def _local(self, absolute: QRectF) -> QRect:
        return absolute.translated(-self.ORIGIN).toRect()

    def _absolute(self, widget: QWidget, origin: QPointF | None = None) -> QRectF:
        """`widget`'s geometry, window-local in, absolute logical out."""
        return QRectF(widget.geometry()).translated(self.ORIGIN if origin is None else origin)

    def _inside(self, usable: QRectF) -> QRectF:
        """Where a bar kept `BAR_EDGE_MARGIN` inside `usable` may be."""
        return usable.adjusted(self.MARGIN, self.MARGIN, -self.MARGIN, -self.MARGIN)

    def _take_whole(self, overlay: OverlayWindow, monitor: QRectF) -> None:
        overlay.set_selection(self._local(monitor))
        assert overlay._bar.isVisible()

    def _store(self, text: str) -> None:
        path = setup_desktop.config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    # -- with no room, it stays on the selection's monitor --------------------

    @pytest.mark.parametrize(
        "monitor", [ABOVE_MAIN, ABOVE_SECOND, ABOVE_TOP], ids=["main", "second", "mounted-above"]
    )
    def test_a_whole_monitor_keeps_the_bar_on_it_against_its_bottom(self, monkeypatch, monitor):
        # #79: it was sent to the nearest other monitor, a bezel away from the
        # work, and reported as bad UX.
        overlay = self._overlay(monkeypatch, pointer_on=monitor)

        self._take_whole(overlay, monitor)

        bar = self._absolute(overlay._bar)
        usable = overlay._usable_area(monitor)
        assert self._inside(usable).contains(bar), f"bar at {bar}"
        assert bar.bottom() == usable.bottom() - self.MARGIN
        # Centred on the selection, which a dock down one side does not move.
        assert abs(bar.center().x() - monitor.center().x()) <= 1
        assert overlay._bar_bounds() == overlay._chrome_bounds()

    def test_a_drag_is_offered_no_monitor_the_selection_reaches_into(self, monkeypatch):
        overlay = self._overlay(monkeypatch)
        # All of the main monitor's height, and on into the second monitor,
        # which would otherwise be the nearest.
        selection = QRectF(0, 0, 3000, 1440)

        overlay.set_selection(self._local(selection))

        bar = self._absolute(overlay._bar)
        assert self._inside(ABOVE_MAIN).contains(bar), f"bar at {bar}"
        assert overlay._bar_elsewhere() == overlay._to_local_rect(ABOVE_TOP)

    def test_a_monitor_too_small_to_hold_the_bar_is_not_offered_to_a_drag(self, monkeypatch):
        # A dock leaving the second monitor narrower than the bar.
        overlay = self._overlay(monkeypatch, docks=[(ABOVE_SECOND, QMargins(2300, 0, 0, 0))])

        self._take_whole(overlay, ABOVE_MAIN)

        assert overlay._bar_elsewhere() == overlay._to_local_rect(ABOVE_TOP)

    # -- what follows it onto another monitor, and what does not -------------

    def test_everything_hung_off_the_bar_follows_it_inside_that_monitors_usable_area(
        self, monkeypatch
    ):
        overlay = self._overlay(monkeypatch)
        self._take_whole(overlay, ABOVE_MAIN)
        _drag_bar(overlay._bar, QPointF(2000, -400))
        usable = overlay._usable_area(ABOVE_SECOND)
        assert usable == ABOVE_SECOND.marginsRemoved(QMarginsF(self.DOCK))

        def assert_inside(name: str, widget: QWidget) -> None:
            assert widget.isVisible(), f"{name} is not showing"
            rect = self._absolute(widget)
            assert usable.contains(rect), f"{name} at {rect}, outside {usable}"

        bar = self._absolute(overlay._bar)
        assert self._inside(usable).contains(bar), f"bar at {bar}"

        overlay._bar.select_tool("pen")
        assert_inside("tool hint", overlay._tool_hint)
        for family in ("shapes", "redact"):
            overlay._toggle_family_menu(family)
            assert_inside(f"{family} menu", overlay._family_menus[family])
        overlay._toggle_style()
        assert_inside("style popover", overlay._style_popover)
        overlay._toggle_watermark_menu()
        assert_inside("watermark menu", overlay._watermark_menu)
        overlay._toggle_capture_popover()
        assert_inside("capture popover", overlay._popover)

        # And nothing else that is up is left behind in a gap, whatever the
        # bar comes to carry. What stays with the capture is checked on the
        # capture's monitor in the test below; the chooser itself draws
        # nothing.
        stays = {
            overlay._close_button,
            overlay._toast,
            overlay._chooser,
            overlay._chooser.tab,
        }
        for child in overlay.findChildren(
            QWidget, options=Qt.FindChildOption.FindDirectChildrenOnly
        ):
            if child.isVisible() and not child.isWindow() and child not in stays:
                assert_inside(type(child).__name__, child)

    def test_what_belongs_to_the_capture_stays_on_the_captures_monitor(self, monkeypatch):
        overlay = self._overlay(monkeypatch)
        self._take_whole(overlay, ABOVE_MAIN)
        _drag_bar(overlay._bar, QPointF(2000, -400))
        assert ABOVE_SECOND.contains(self._absolute(overlay._bar))

        overlay._show_toast("save", "Saved")

        for name, widget in (
            ("close button", overlay._close_button),
            ("toast", overlay._toast),
            ("chooser tab", overlay._chooser.tab),
        ):
            assert widget.isVisible(), name
            rect = self._absolute(widget)
            assert ABOVE_MAIN.contains(rect), f"{name} at {rect}"

    # -- room beside the selection, and a single monitor -----------------------

    def test_room_beside_the_selection_still_wins(self, monkeypatch):
        setup_desktop.save_bar_position(setup_desktop.BarPosition(self.OTHER, (0.5, 0.5)))
        overlay = self._overlay(monkeypatch)
        selection = QRectF(700, 400, 1200, 700)

        overlay.set_selection(self._local(selection))

        bar = self._absolute(overlay._bar)
        assert ABOVE_MAIN.contains(bar)
        assert bar.top() == selection.bottom() + self.OFFSET
        assert overlay._bar_bounds() == overlay._chrome_bounds()

    def test_a_drag_beside_a_selection_with_room_stays_on_its_monitor(self, monkeypatch):
        overlay = self._overlay(monkeypatch)
        overlay.set_selection(self._local(QRectF(700, 400, 1200, 700)))

        _drag_bar(overlay._bar, QPointF(3000, 0))

        assert self._inside(ABOVE_MAIN).contains(self._absolute(overlay._bar))
        assert setup_desktop.load_bar_position() is None

    def test_on_a_single_monitor_it_sits_where_it_always_has(self, monkeypatch):
        overlay = self._single(monkeypatch)
        origin = QPointF(ABOVE_TOP.topLeft())

        bar = self._absolute(overlay._bar, origin)

        assert overlay._bar_elsewhere() is None
        assert bar.bottom() == ABOVE_TOP.bottom() - self.MARGIN
        assert self._inside(ABOVE_TOP).contains(bar)

    def test_on_a_single_monitor_a_drag_is_remembered_as_its_own(self, monkeypatch):
        overlay = self._single(monkeypatch)

        _drag_bar(overlay._bar, QPointF(-600, -900))

        assert setup_desktop.load_bar_position().monitor == self.OWN

    def test_on_a_single_monitor_a_place_remembered_on_the_other_keeps_its_spot(
        self, monkeypatch
    ):
        setup_desktop.save_bar_position(setup_desktop.BarPosition(self.OTHER, (1.0, 0.0)))

        overlay = self._single(monkeypatch)

        bar = self._absolute(overlay._bar, QPointF(ABOVE_TOP.topLeft()))
        assert bar.topRight() == self._inside(ABOVE_TOP).topRight()

    # -- a drag is remembered, and wins next time ------------------------------

    def test_a_drag_onto_the_other_monitor_is_remembered_and_wins_next_time(self, monkeypatch):
        first = self._overlay(monkeypatch)
        self._take_whole(first, ABOVE_MAIN)

        _drag_bar(first._bar, QPointF(2000, -400))

        dragged = self._absolute(first._bar)
        assert self._inside(first._usable_area(ABOVE_SECOND)).contains(dragged)
        assert setup_desktop.load_bar_position().monitor == self.OTHER
        second = self._overlay(monkeypatch)
        self._take_whole(second, ABOVE_MAIN)
        assert self._absolute(second._bar) == dragged

    def test_a_drag_on_the_selections_own_monitor_is_remembered_and_wins_next_time(
        self, monkeypatch
    ):
        first = self._overlay(monkeypatch)
        self._take_whole(first, ABOVE_MAIN)
        first._bar.select_tool("pen")
        before = self._absolute(first._bar)

        _drag_bar(first._bar, QPointF(-600, -500))

        dragged = self._absolute(first._bar)
        assert dragged.topLeft() == before.topLeft() + QPointF(-600, -500)
        assert self._inside(ABOVE_MAIN).contains(dragged)
        # The strip naming the tool crossed with it.
        hint = self._absolute(first._tool_hint)
        assert first._tool_hint.isVisible()
        assert ABOVE_MAIN.contains(hint), f"hint at {hint}"
        assert setup_desktop.load_bar_position().monitor == self.OWN

        second = self._overlay(monkeypatch)
        self._take_whole(second, ABOVE_MAIN)
        assert self._absolute(second._bar) == dragged

    def test_a_drag_from_a_remembered_place_onto_the_other_monitor_wins_next_time(
        self, monkeypatch
    ):
        setup_desktop.save_bar_position(setup_desktop.BarPosition(self.OWN, (0.5, 0.5)))
        first = self._overlay(monkeypatch)
        self._take_whole(first, ABOVE_MAIN)
        # The remembered place wins over the placement at the bottom.
        assert self._inside(ABOVE_MAIN).contains(self._absolute(first._bar))

        _drag_bar(first._bar, QPointF(2000, 0))

        dragged = self._absolute(first._bar)
        assert self._inside(first._usable_area(ABOVE_SECOND)).contains(dragged)
        assert setup_desktop.load_bar_position().monitor == self.OTHER
        second = self._overlay(monkeypatch)
        self._take_whole(second, ABOVE_MAIN)
        assert self._absolute(second._bar) == dragged

    def test_a_drag_is_never_left_in_a_gap_nor_on_a_third_monitor(self, monkeypatch):
        overlay = self._overlay(monkeypatch)
        self._take_whole(overlay, ABOVE_MAIN)
        bar = overlay._bar
        press = bar.mapToParent(_grip(bar))
        _bar_pointer(bar, "press", _grip(bar), self.LEFT)

        def carry_to(offset: QPointF) -> QRectF:
            _bar_pointer(bar, "move", bar.mapFromParent(press + offset), self.LEFT)
            return self._absolute(bar)

        # Up past the second monitor's top, into the gap right of the monitor
        # mounted above: the bar stops at the second monitor's top bar.
        in_the_gap = carry_to(QPointF(3000, -2200))
        assert self._inside(overlay._usable_area(ABOVE_SECOND)).contains(in_the_gap)
        assert in_the_gap.top() == ABOVE_SECOND.top() + self.DOCK.top() + self.MARGIN

        # On over the monitor mounted above, which the bar is never sent to:
        # it lands on the nearer of its two, the main monitor, below it.
        over_the_third = carry_to(QPointF(0, -2200))
        assert self._inside(ABOVE_MAIN).contains(over_the_third), f"bar at {over_the_third}"
        assert not over_the_third.intersects(ABOVE_TOP)

        _bar_pointer(
            bar, "release", bar.mapFromParent(press + QPointF(0, -2200)), Qt.MouseButton.NoButton
        )
        assert setup_desktop.load_bar_position().monitor == self.OWN

    # -- the stored value -------------------------------------------------------

    def test_a_place_saved_before_the_bar_could_change_monitor_keeps_it_on_its_own(
        self, monkeypatch
    ):
        self._store('{"bar_position": [1.0, 0.0]}')
        overlay = self._overlay(monkeypatch)

        self._take_whole(overlay, ABOVE_MAIN)

        bar = self._absolute(overlay._bar)
        assert bar.topRight() == self._inside(ABOVE_MAIN).topRight()

    def test_a_stored_place_it_cannot_read_leaves_the_placement_automatic(self, monkeypatch):
        self._store('{"bar_position": {"monitor": "beside", "spot": [1.0, 0.0]}}')
        overlay = self._overlay(monkeypatch)

        self._take_whole(overlay, ABOVE_MAIN)

        bar = self._absolute(overlay._bar)
        usable = overlay._usable_area(ABOVE_MAIN)
        assert self._inside(usable).contains(bar), f"bar at {bar}"
        assert bar.bottom() == usable.bottom() - self.MARGIN

    # -- the export -------------------------------------------------------------

    def test_the_export_is_the_monitor_and_nothing_of_the_chrome(self, monkeypatch):
        overlay = self._overlay(monkeypatch)
        self._take_whole(overlay, ABOVE_MAIN)
        overlay._bar.select_tool("pen")
        overlay._toggle_style()
        untouched = QImage(2560, 1440, QImage.Format.Format_RGB32)
        untouched.fill(BASE_COLOR)

        rendered = overlay.rendered_image()

        assert rendered.size() == untouched.size()
        assert rendered.convertToFormat(QImage.Format.Format_RGB32) == untouched

        # Dragged across the selection, still none of it.
        _drag_bar(overlay._bar, QPointF(-600, -500))
        assert self._inside(ABOVE_MAIN).contains(self._absolute(overlay._bar))
        rendered = overlay.rendered_image()
        assert rendered.convertToFormat(QImage.Format.Format_RGB32) == untouched


class TestFullScreenFollowsThePointerBeforeItCommits:
    """#53: on a desk with more than one monitor Full screen arms, previews
    the monitor under the pointer and takes the one clicked -- the loop
    Window mode already has. On a desk with one it still captures the moment
    it is picked.

    On #49's desk, so the monitor mounted above -- and its negative origin --
    is one of the monitors being crossed into.
    """

    @pytest.fixture(autouse=True)
    def _clean_slate(self):
        _close_stray_toplevel_windows()

    ORIGIN = QPointF(*MOUNTED_ABOVE_ORIGIN)

    def _overlay(self, monkeypatch, pointer_on=ABOVE_SECOND, *, after="edit", **kwargs):
        _point_the_os_at(monkeypatch, pointer_on)
        frame = make_frame(
            image_size=MOUNTED_ABOVE_SIZE,
            logical_size=MOUNTED_ABOVE_SIZE,
            logical_origin=MOUNTED_ABOVE_ORIGIN,
        )
        overlay = OverlayWindow(frame, monitor_geometries=list(MOUNTED_ABOVE), **kwargs)
        overlay._chooser.set_after(after)
        overlay.show()
        QTest.qWaitForWindowExposed(overlay)
        return overlay

    def _hover(self, overlay, absolute: QPointF) -> None:
        # Sent to the overlay itself rather than synthesised with QTest: a
        # bare hover goes to whichever window the platform thinks is under
        # the pointer, which offscreen is not reliably this one.
        local = absolute - self.ORIGIN
        QApplication.sendEvent(
            overlay,
            QMouseEvent(
                QEvent.Type.MouseMove, local, local,
                Qt.MouseButton.NoButton, Qt.MouseButton.NoButton,
                Qt.KeyboardModifier.NoModifier,
            ),
        )

    def _click(self, overlay, absolute: QPointF) -> None:
        QTest.mouseClick(
            overlay, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
            (absolute - self.ORIGIN).toPoint(),
        )

    def test_picking_it_arms_rather_than_captures(self, monkeypatch):
        copied = []
        monkeypatch.setattr(output_module, "copy_image_to_clipboard", copied.append)
        monkeypatch.setattr(setup_desktop, "load_instant_saves", lambda: False)
        overlay = self._overlay(monkeypatch, after="instant")

        overlay._chooser.set_mode("Full screen")

        assert overlay._picking_monitor
        assert overlay._selection is None
        # The row stays up while the monitor under the pointer is previewed:
        # picking a mode never arms it (#66).
        assert overlay._chooser.phase == "choosing"
        assert copied == []
        assert overlay.isVisible()

    def test_the_preview_opens_on_the_monitor_the_pointer_is_on(self, monkeypatch):
        overlay = self._overlay(monkeypatch, ABOVE_TOP)

        overlay._chooser.set_mode("Full screen")

        assert overlay._hovered_monitor == ABOVE_TOP

    def test_moving_across_a_bezel_previews_the_new_monitor(self, monkeypatch):
        overlay = self._overlay(monkeypatch)
        overlay._chooser.set_mode("Full screen")

        for monitor in (ABOVE_TOP, ABOVE_MAIN, ABOVE_SECOND):
            self._hover(overlay, monitor.center())
            assert overlay._hovered_monitor == monitor

    def test_a_gap_between_monitors_previews_nothing(self, monkeypatch):
        # Above the main monitor and left of the one mounted above.
        overlay = self._overlay(monkeypatch)
        overlay._chooser.set_mode("Full screen")

        self._hover(overlay, QPointF(500, -700))

        assert overlay._hovered_monitor is None
        assert overlay._picking_monitor

    def test_clicking_takes_the_previewed_monitor(self, monkeypatch):
        overlay = self._overlay(monkeypatch)
        overlay._chooser.set_mode("Full screen")
        self._hover(overlay, ABOVE_TOP.center())

        self._click(overlay, ABOVE_TOP.center())

        assert overlay.absolute_selection() == ABOVE_TOP
        assert not overlay._picking_monitor
        assert overlay._hovered_monitor is None

    def test_a_click_in_a_gap_takes_nothing_and_stays_armed(self, monkeypatch):
        overlay = self._overlay(monkeypatch)
        overlay._chooser.set_mode("Full screen")

        self._click(overlay, QPointF(500, -700))

        assert overlay._selection is None
        assert overlay._picking_monitor

    def test_instant_finishes_on_the_click_not_on_the_pick(self, monkeypatch):
        copied = []
        monkeypatch.setattr(output_module, "copy_image_to_clipboard", copied.append)
        monkeypatch.setattr(setup_desktop, "load_instant_saves", lambda: False)
        overlay = self._overlay(monkeypatch, after="instant")
        overlay._on_captured = lambda image, path: None
        overlay._chooser.set_mode("Full screen")
        assert copied == []

        self._click(overlay, ABOVE_MAIN.center())

        assert len(copied) == 1
        assert not overlay.isVisible()
        # What was taken is remembered absolute: the monitor clicked, not
        # the one the pointer was on when Full screen was chosen.
        assert setup_desktop.load_last_region() == (0, 0, 2560, 1440)

    def test_escape_leaves_the_mode_without_capturing(self, monkeypatch):
        reported = []
        overlay = self._overlay(
            monkeypatch, on_captured=lambda image, path: reported.append(path)
        )
        overlay._chooser.set_mode("Full screen")

        QTest.keyClick(overlay, Qt.Key.Key_Escape)

        assert not overlay._picking_monitor
        assert overlay._hovered_monitor is None
        assert overlay._selection is None
        assert overlay._chooser.phase == "choosing"
        assert overlay.isVisible()
        assert reported == []

    def test_the_next_escape_leaves_the_snip(self, monkeypatch):
        overlay = self._overlay(monkeypatch)
        overlay._chooser.set_mode("Full screen")

        QTest.keyClick(overlay, Qt.Key.Key_Escape)
        QTest.keyClick(overlay, Qt.Key.Key_Escape)

        assert not overlay.isVisible()

    def test_the_chooser_row_follows_the_pointer_while_it_is_armed(self, monkeypatch):
        # Gated on there being no selection yet, which a snap never left.
        overlay = self._overlay(monkeypatch)
        overlay._chooser.set_mode("Full screen")

        self._hover(overlay, ABOVE_TOP.center())

        row = QRectF(overlay._chooser.row.geometry()).translated(self.ORIGIN)
        assert overlay._chooser.row.isVisible()
        assert ABOVE_TOP.contains(row), f"row at {row}"

    def test_choosing_another_mode_disarms_it(self, monkeypatch):
        overlay = self._overlay(monkeypatch)
        overlay._chooser.set_mode("Full screen")

        overlay._chooser.set_mode("Region")

        assert not overlay._picking_monitor
        assert overlay._hovered_monitor is None

    def test_choosing_it_from_a_selection_already_made_arms_it_too(self, monkeypatch):
        # The mode menu reached from the bar rather than the chooser.
        overlay = self._overlay(monkeypatch)
        overlay.set_selection(QRect(3000, 1700, 400, 300))

        overlay._on_capture_mode_selected("Full screen")

        assert overlay._picking_monitor
        assert overlay._selection is None

    def test_on_the_record_side_the_monitor_clicked_is_the_one_recorded(self, monkeypatch):
        requests = []
        overlay = self._overlay(
            monkeypatch,
            on_recording_requested=lambda rect, delay, after: requests.append(rect),
        )
        overlay._chooser.set_kind("record")
        overlay._chooser.set_mode("Full screen")
        assert requests == []
        assert overlay._picking_monitor

        self._click(overlay, ABOVE_TOP.center())

        assert requests == [ABOVE_TOP]
        assert overlay._armed_for_recording

    def test_on_a_single_monitor_it_still_captures_the_moment_it_is_picked(
        self, monkeypatch
    ):
        _point_the_os_at(monkeypatch, ABOVE_TOP)
        frame = make_frame(
            image_size=(2560, 1440), logical_size=(2560, 1440), logical_origin=(1164, -1440)
        )
        overlay = OverlayWindow(frame, monitor_geometries=[ABOVE_TOP])
        overlay._chooser.set_after("edit")
        overlay.show()
        QTest.qWaitForWindowExposed(overlay)

        overlay._chooser.set_mode("Full screen")

        assert not overlay._picking_monitor
        assert overlay.absolute_selection() == ABOVE_TOP

    def test_the_preview_is_window_modes_highlight(self, monkeypatch):
        # A small desk, painted unshown, so the grab holds nothing but the
        # frame, the scrim and the highlight.
        left, right = QRectF(0, 0, 300, 200), QRectF(300, 0, 300, 200)
        _point_the_os_at(monkeypatch, right)
        overlay = OverlayWindow(
            make_frame(image_size=(600, 200), logical_size=(600, 200)),
            monitor_geometries=[left, right],
        )
        overlay._chooser.set_mode("Full screen")
        assert overlay._hovered_monitor == right
        previewing_monitor = overlay.grab().toImage()

        overlay._picking_monitor = False
        overlay._picking_window = True
        overlay._hovered_window = ("", right)
        previewing_window = overlay.grab().toImage()

        def at(image, x, y):
            ratio = image.devicePixelRatio()
            return image.pixelColor(round(x * ratio), round(y * ratio))

        assert at(previewing_monitor, 450, 120) == at(previewing_window, 450, 120)
        assert at(previewing_monitor, 450, 120) != at(previewing_monitor, 150, 120)


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
        # A provider that can answer, so the Window mode these tests pick
        # really arms rather than falling back to Region.
        overlay = OverlayWindow(
            frame, geometry_provider=_FakeWindowProvider(QRectF(0, 0, 100, 100))
        )
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

    @pytest.fixture(autouse=True)
    def _clean_slate(self):
        # An open menu is a popup, which takes every mouse event in the
        # process until it closes.
        _close_stray_toplevel_windows()
        yield
        _close_stray_toplevel_windows()

    @pytest.mark.parametrize(
        "control", ["stills", "record", "mode_chip", "destination", "hide_flag", "delay_flag"]
    )
    def test_a_press_on_a_control_starts_no_capture(self, control):
        overlay = self._overlay()

        self._press(getattr(overlay._chooser.row, control))

        # A selection at all -- even the empty one a drag opens with -- is
        # what folds the row and puts the floating bar up.
        assert overlay._selection is None
        assert overlay._chooser.row.isVisibleTo(overlay)

    def test_a_press_on_the_row_itself_starts_no_capture(self):
        # The gaps between the controls and the wells' padding are the row's
        # own background, and a press that lands there is still a press on
        # the chooser.
        overlay = self._overlay()

        self._press(overlay._chooser.row)
        self._press(overlay._chooser.row.kind_well)

        assert overlay._selection is None
        assert overlay._chooser.row.isVisibleTo(overlay)

    def test_a_press_on_the_tab_starts_no_new_capture(self):
        overlay = self._overlay()
        overlay.set_selection(QRect(100, 200, 300, 200))

        self._press(overlay._chooser.tab)

        assert overlay._selection == QRect(100, 200, 300, 200)
        assert overlay._chooser.tab.isVisibleTo(overlay)

    def test_clicking_the_mode_chip_opens_its_menu(self):
        overlay = self._overlay()

        self._click(overlay._chooser.row.mode_chip)

        assert overlay._chooser._menu is not None

    def test_clicking_through_to_a_row_picks_that_mode(self):
        # The whole gesture the video showed failing: click the control,
        # then click a row in the menu it opened. Window, which this class's
        # overlay has a provider for -- without one it would fall back to
        # Region and prove nothing about the click that got there.
        overlay = self._overlay()

        self._click(overlay._chooser.row.mode_chip)
        self._click(overlay._chooser._menu._rows["Window"])

        assert overlay._chooser.mode == "Window"
        assert overlay._chooser.phase == "choosing"
        assert overlay._capture_mode == "Window"
        assert overlay._picking_window

    def test_clicking_the_tab_reopens_the_row(self):
        overlay = self._overlay()
        overlay.set_selection(QRect(100, 200, 300, 200))

        self._click(overlay._chooser.tab)

        assert overlay._chooser.phase == "choosing"

    def test_sliding_off_a_control_before_releasing_is_not_a_click(self):
        # Consuming the press makes this widget Qt's implicit mouse
        # grabber, so the release comes back here wherever it happens.
        # Pressing a control and sliding away from it means "no".
        overlay = self._overlay()
        chip = overlay._chooser.row.mode_chip

        self._press(chip)
        QTest.mouseRelease(chip, Qt.MouseButton.LeftButton, pos=QPoint(-200, 400))

        assert overlay._chooser._menu is None

    def test_sliding_off_a_menu_row_before_releasing_picks_nothing(self):
        overlay = self._overlay()
        self._click(overlay._chooser.row.mode_chip)
        row = overlay._chooser._menu._rows["Window"]

        QTest.mousePress(row, Qt.MouseButton.LeftButton, pos=self._centre(row))
        QTest.mouseRelease(row, Qt.MouseButton.LeftButton, pos=QPoint(-200, 400))

        assert overlay._chooser.mode == "Region"
        assert overlay._chooser.phase == "choosing"
        assert overlay._capture_mode == "Region"

    def test_double_clicking_a_control_starts_no_capture(self):
        # Qt sends a second press as a `MouseButtonDblClick`, and a widget
        # that ignores that gets the press-propagating default back --
        # which is the same leak by another event type. Clicking twice
        # because nothing seemed to happen is exactly how the bug was hit.
        overlay = self._overlay()
        chip = overlay._chooser.row.mode_chip

        QTest.mouseDClick(chip, Qt.MouseButton.LeftButton, pos=self._centre(chip))

        assert overlay._selection is None
        assert overlay._chooser.row.isVisibleTo(overlay)


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
        monkeypatch.setattr(output_module, "copy_image_to_clipboard", lambda image: None)
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


class TestPinDestination:
    """SNX-83: Pin joins Copy/Save/Open as a fourth ending on the
    destination menu -- `OverlayWindow._on_bar_pin`/`_open_destination_menu`.
    """

    def _overlay(self, size=(800, 600), selection=QRect(100, 100, 300, 250)) -> OverlayWindow:
        frame = make_frame(image_size=size, logical_size=size)
        overlay = OverlayWindow(frame)
        overlay.setGeometry(0, 0, *size)
        overlay.set_selection(selection)
        return overlay

    def test_pin_hands_the_render_and_the_selections_absolute_rect_to_the_callback(self):
        overlay = self._overlay()
        seen = []
        overlay._on_pin_requested = lambda image, rect: seen.append((image, rect))

        overlay._on_bar_pin()

        assert len(seen) == 1
        image, rect = seen[0]
        assert image.size() == overlay.rendered_image().size()
        # The frame's own logical origin is (0, 0) here, so the absolute
        # rect is the selection unchanged.
        assert rect == QRect(100, 100, 300, 250)

    def test_pin_closes_the_overlay(self):
        overlay = self._overlay()
        overlay._on_pin_requested = lambda image, rect: None

        overlay._on_bar_pin()

        assert overlay.isHidden()

    def test_pin_never_opens_a_review_window(self):
        # Pin has no `tokens.AFTER_CAPTURE` outcome, so `outcome` is
        # whatever it already was -- possibly "review", left over from an
        # earlier choice. `_on_captured` (the review window's own trigger)
        # must not fire at all for a pin, or a leftover "review" outcome
        # would open one alongside the pin.
        overlay = self._overlay()
        overlay._chooser.set_after("review")
        overlay._on_pin_requested = lambda image, rect: None
        captured = Mock()
        overlay._on_captured = captured

        overlay._on_bar_pin()

        captured.assert_not_called()

    def test_pin_remembers_the_last_region(self, monkeypatch):
        remembered = []
        monkeypatch.setattr(
            setup_desktop, "save_last_region", lambda rect: remembered.append(rect)
        )
        overlay = self._overlay()
        overlay._on_pin_requested = lambda image, rect: None

        overlay._on_bar_pin()

        assert remembered == [(100, 100, 300, 250)]

    def test_the_menus_pin_row_is_live_when_the_platform_can_pin(self, monkeypatch):
        monkeypatch.setattr(overlay_module.platform.current, "can_pin", lambda: True)
        overlay = self._overlay()

        overlay._open_destination_menu()

        rows = {value: reason for value, _label, _note, _key, reason in overlay._destination_menu._rows}
        assert rows["Pin"] == ""
        overlay._destination_menu.close()

    def test_the_menus_pin_row_is_greyed_with_its_reason_when_the_platform_cannot(
        self, monkeypatch
    ):
        monkeypatch.setattr(overlay_module.platform.current, "can_pin", lambda: False)
        monkeypatch.setattr(
            overlay_module.platform.current, "pin_unavailable_reason", lambda: "no Wayland"
        )
        overlay = self._overlay()

        overlay._open_destination_menu()

        rows = {value: reason for value, _label, _note, _key, reason in overlay._destination_menu._rows}
        assert rows["Pin"] == "no Wayland"
        overlay._destination_menu.close()

    def test_choosing_pin_from_the_menu_fires_it(self):
        overlay = self._overlay()
        seen = []
        overlay._on_pin_requested = lambda image, rect: seen.append(rect)

        overlay._on_destination_chosen("Pin")

        assert seen == [QRect(100, 100, 300, 250)]
        # Unlike Copy/Save/Open, Pin has no outcome to write back.
        assert overlay.outcome != "pin"


class TestTheCursorInvitesTheDrag:
    """With nothing selected the frozen frame takes a drag whatever mode is
    showing. Picking a mode no longer arms it (#66), so the crosshair is up
    from the moment the overlay opens, and the row keeps its own arrow.
    """

    def _overlay(self, size=(1200, 800)):
        frame = make_frame(image_size=size, logical_size=size)
        overlay = OverlayWindow(frame)
        overlay.setGeometry(0, 0, *size)
        overlay.show()
        QTest.qWaitForWindowExposed(overlay)
        return overlay

    def test_an_overlay_opens_on_a_crosshair(self):
        overlay = self._overlay()

        assert overlay._chooser.phase == "choosing"
        assert overlay.cursor().shape() == Qt.CursorShape.CrossCursor

    def test_the_row_keeps_an_ordinary_arrow(self):
        # The row is a thing to click, not an area to drag across.
        overlay = self._overlay()

        assert overlay._chooser.row.cursor().shape() == Qt.CursorShape.ArrowCursor

    def test_picking_region_keeps_the_crosshair_and_the_row(self):
        overlay = self._overlay()

        overlay._chooser.set_mode("Region")

        assert overlay._chooser.phase == "choosing"
        assert overlay.cursor().shape() == Qt.CursorShape.CrossCursor

    def test_a_selection_takes_the_cursor_back_over(self):
        # Once there is a rectangle, the handle and inside-the-selection
        # rules own the pointer; the idle crosshair must not override them.
        overlay = self._overlay()
        overlay._chooser.set_mode("Region")

        overlay.set_selection(QRect(100, 100, 300, 200))
        overlay._apply_idle_cursor()

        assert overlay.cursor().shape() == Qt.CursorShape.ArrowCursor


class TestTheChooserFoldsToATabOnceSomethingIsSelected:
    """#66: the row stays up until a selection exists, then folds to a 22px
    tab on the same edge. The tab or Space opens the row again over the
    selection, with everything as it was.
    """

    @pytest.fixture(autouse=True)
    def _clean_slate(self):
        _close_stray_toplevel_windows()

    WINDOW = QRectF(400, 300, 200, 200)

    def _overlay(self, size=(1200, 800), **kwargs):
        frame = make_frame(image_size=size, logical_size=size)
        kwargs.setdefault("geometry_provider", _FakeWindowProvider(self.WINDOW))
        overlay = OverlayWindow(frame, **kwargs)
        overlay.setGeometry(0, 0, *size)
        overlay.show()
        QTest.qWaitForWindowExposed(overlay)
        return overlay

    @staticmethod
    def _drag(overlay, start=QPoint(700, 300), end=QPoint(1000, 600)):
        QTest.mousePress(overlay, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, start)
        QTest.mouseMove(overlay, end)
        QTest.mouseRelease(overlay, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, end)

    @staticmethod
    def _click(widget):
        QTest.mouseClick(
            widget, Qt.MouseButton.LeftButton,
            pos=QPoint(widget.width() // 2, widget.height() // 2),
        )

    @staticmethod
    def _showing(overlay):
        chooser = overlay._chooser
        return {
            name: getattr(chooser, name).isVisibleTo(overlay)
            for name in ("row", "hint", "tab")
        }

    def test_the_row_and_its_pill_are_up_while_nothing_is_selected(self):
        overlay = self._overlay()

        assert self._showing(overlay) == {"row": True, "hint": True, "tab": False}

    def test_a_drag_folds_the_row_to_its_tab(self):
        overlay = self._overlay()

        self._drag(overlay)

        assert overlay._chooser.phase == "collapsed"
        assert self._showing(overlay) == {"row": False, "hint": False, "tab": True}

    def test_the_tab_hangs_from_the_rows_own_edge(self):
        overlay = self._overlay()
        row = overlay._chooser.row.geometry()

        overlay.set_selection(QRect(700, 300, 300, 300))

        tab = overlay._chooser.tab.geometry()
        assert tab.top() == row.top()
        assert abs(tab.center().x() - row.center().x()) <= 1

    def test_hovering_a_window_is_not_a_selection(self):
        # Nothing is chosen until the click, so the row stays up through the
        # preview even though the preview is drawn as a selection.
        overlay = self._overlay()
        overlay._chooser.set_mode("Window")

        point = self.WINDOW.center()
        QApplication.sendEvent(
            overlay,
            QMouseEvent(
                QEvent.Type.MouseMove, point, point,
                Qt.MouseButton.NoButton, Qt.MouseButton.NoButton,
                Qt.KeyboardModifier.NoModifier,
            ),
        )

        assert overlay._selection is not None
        assert overlay._chooser.phase == "choosing"
        assert self._showing(overlay)["row"]

    def test_clicking_the_window_folds_it(self):
        overlay = self._overlay()
        overlay._chooser.set_mode("Window")

        QTest.mouseClick(
            overlay, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
            self.WINDOW.center().toPoint(),
        )

        assert overlay._selection == self.WINDOW.toRect()
        assert overlay._chooser.phase == "collapsed"

    def test_the_tab_reopens_the_row_over_the_selection_it_leaves_alone(self):
        overlay = self._overlay()
        self._drag(overlay)
        selection = overlay._selection

        self._click(overlay._chooser.tab)

        assert overlay._chooser.phase == "choosing"
        assert self._showing(overlay)["row"]
        assert overlay._selection == selection
        assert overlay._bar.isVisible()

    def test_space_reopens_it_and_folds_it_again(self):
        overlay = self._overlay()
        self._drag(overlay)
        selection = overlay._selection

        QTest.keyClick(overlay, Qt.Key.Key_Space)
        assert overlay._chooser.phase == "choosing"

        QTest.keyClick(overlay, Qt.Key.Key_Space)
        assert overlay._chooser.phase == "collapsed"
        assert overlay._selection == selection

    def test_escape_folds_a_reopened_row_before_anything_else(self):
        overlay = self._overlay()
        self._drag(overlay)
        selection = overlay._selection
        QTest.keyClick(overlay, Qt.Key.Key_Space)

        QTest.keyClick(overlay, Qt.Key.Key_Escape)

        assert overlay._chooser.phase == "collapsed"
        assert overlay._selection == selection
        assert overlay.isVisible()

    def test_a_press_on_the_frame_folds_a_reopened_row(self):
        overlay = self._overlay()
        self._drag(overlay)
        QTest.keyClick(overlay, Qt.Key.Key_Space)

        QTest.mousePress(
            overlay, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(150, 600)
        )

        assert overlay._chooser.phase == "collapsed"
        QTest.mouseRelease(
            overlay, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(150, 600)
        )

    def test_the_reopened_row_can_change_what_the_bar_does(self):
        overlay = self._overlay()
        self._drag(overlay)
        QTest.keyClick(overlay, Qt.Key.Key_Space)
        assert overlay._bar.destination() == "Copy"  # edit, the default

        self._click(overlay._chooser.row.destination)

        assert overlay._chooser.after == "save"
        assert overlay._bar.destination() == "Save"

    def test_clearing_the_selection_brings_the_row_back(self):
        overlay = self._overlay()
        self._drag(overlay)

        QTest.keyClick(overlay, Qt.Key.Key_Escape)

        assert overlay._selection is None
        assert overlay._chooser.phase == "choosing"
        assert self._showing(overlay)["row"]

    def test_it_is_out_of_the_way_while_a_recording_is_armed(self):
        # app.py holds that state; a mode picked from a reopened row would
        # pull the region out from under it.
        overlay = self._overlay(on_recording_requested=lambda rect, delay, after: None)
        overlay._chooser.set_kind("record")

        self._drag(overlay)

        assert overlay._armed_for_recording
        assert self._showing(overlay) == {"row": False, "hint": False, "tab": False}


# ---------------------------------------------------------------------------
# The watermark (#69)
# ---------------------------------------------------------------------------

WATERMARK_MAGENTA = QColor(255, 0, 255)


def _keep_watermark_image(tmp_path, size=(400, 200), data: bytes | None = None) -> None:
    """Settings' Image, kept the way Save keeps it, in this test's own config:
    a solid magenta picture of `size` pixels, or `data` as the file."""
    source = tmp_path / "pictures" / "logo.png"
    source.parent.mkdir(parents=True, exist_ok=True)
    if data is None:
        image = QImage(*size, QImage.Format.Format_ARGB32)
        image.fill(WATERMARK_MAGENTA)
        assert image.save(str(source))
    else:
        source.write_bytes(data)
    assert setup_desktop.save_watermark_image(source)
    setup_desktop.save_watermark_kind("image")


def _switch_watermark_on(corner: str = "br", opacity: int = 100) -> None:
    """What a user leaves behind on an earlier snip this session."""
    session = overlay_module.watermark_session
    session.enabled, session.corner, session.opacity = True, corner, opacity


def _exact_bounds(image: QImage, colour: QColor, region: QRect | None = None) -> QRect:
    """The smallest rect holding every pixel exactly `colour` -- a solid mark's
    extent, less whatever its edges blended -- in `image`'s own pixels."""
    region = region if region is not None else image.rect()
    target = colour.rgb()
    xs, ys = [], []
    for y in range(region.top(), region.bottom() + 1):
        for x in range(region.left(), region.right() + 1):
            if image.pixel(x, y) == target:
                xs.append(x)
                ys.append(y)
    if not xs:
        return QRect()
    return QRect(min(xs), min(ys), max(xs) - min(xs) + 1, max(ys) - min(ys) + 1)


def _physical(logical: QRectF, ratio: float, origin: QPointF = QPointF(0, 0)) -> QRect:
    """`logical`, less `origin`, in pixels `ratio` to a logical pixel."""
    return QRect(
        round((logical.x() - origin.x()) * ratio),
        round((logical.y() - origin.y()) * ratio),
        round(logical.width() * ratio),
        round(logical.height() * ratio),
    )


def _assert_edges_near(found: QRect, expected: QRect, tolerance: int = 1) -> None:
    assert not found.isNull(), f"no mark found where {expected} was expected"
    for edge in ("left", "top", "right", "bottom"):
        assert abs(getattr(found, edge)() - getattr(expected, edge)()) <= tolerance, (
            edge, found, expected
        )


class TestWatermarkSlot:
    """The watermark is a toggle beside the style dot, with a notch -- not a
    tool: docs/design/bars/README.md, "Watermark is not a tool"."""

    @staticmethod
    def _bar(**kwargs) -> FloatingBar:
        bar = FloatingBar(**kwargs)
        bar.resize(bar.sizeHint())
        bar.show()
        QTest.qWaitForWindowExposed(bar)
        return bar

    def test_it_sits_after_the_style_dot_with_the_layers_glyph(self):
        bar = self._bar()

        assert bar._watermark._icon_name == tokens.WATERMARK["glyph"] == "layers"
        assert (
            bar._style_dot.geometry().right()
            < bar._watermark.geometry().left()
            < bar._undo_button.geometry().left()
        )

    def test_its_notch_says_what_it_opens(self):
        assert FloatingBar()._watermark.notch.toolTip() == "Watermark options"

    def test_pressing_it_asks_for_the_toggle_and_arms_no_tool(self):
        bar = self._bar()
        bar.select_tool("pen")
        toggled = Mock()
        bar.watermarkToggled.connect(toggled)

        QTest.mouseClick(bar._watermark, Qt.MouseButton.LeftButton)

        toggled.assert_called_once()
        assert bar.active_tool == "pen"
        assert not bar._watermark.is_active

    def test_its_notch_asks_for_the_menu_and_toggles_nothing(self):
        bar = self._bar()
        toggled, opened = Mock(), Mock()
        bar.watermarkToggled.connect(toggled)
        bar.watermarkMenuRequested.connect(opened)

        QTest.mouseClick(bar._watermark.notch, Qt.MouseButton.LeftButton)

        opened.assert_called_once()
        toggled.assert_not_called()

    def test_its_tooltip_says_whether_it_is_on(self):
        bar = FloatingBar()
        assert bar._watermark.toolTip() == "Watermark is off"

        bar.set_watermark_on(True)

        assert bar._watermark.toolTip() == "Watermark is on — applied on export"

    def test_on_it_wears_the_accent_wash_not_an_armed_tools_white(self):
        bar = FloatingBar()

        bar.set_watermark_on(True)

        background, glyph = bar._watermark._colours(hovered=False)
        assert background == design.watermark_color("ON_BG")
        assert glyph == design.watermark_color("ON_FG")
        assert background != design.bar_color("TOOL_ACTIVE_BG")

    def test_greyed_it_says_why_and_neither_it_nor_its_notch_does_anything(self):
        bar = self._bar()
        bar.set_watermark_unavailable(tokens.WATERMARK_UNSET)
        toggled, opened = Mock(), Mock()
        bar.watermarkToggled.connect(toggled)
        bar.watermarkMenuRequested.connect(opened)

        QTest.mouseClick(bar._watermark, Qt.MouseButton.LeftButton)
        QTest.mouseClick(bar._watermark.notch, Qt.MouseButton.LeftButton)

        toggled.assert_not_called()
        opened.assert_not_called()
        assert bar._watermark.isVisible(), "greyed, not hidden"
        assert bar._watermark.toolTip() == tokens.WATERMARK_UNSET
        assert bar._watermark._colours(hovered=True) == (None, design.bar_color("TOOL_DISABLED_FG"))

    def test_the_review_windows_bar_has_none(self):
        # The snip it opens was already exported by the overlay, watermark
        # and all -- see FloatingBar.__init__.
        assert FloatingBar(trailing="done")._watermark.isHidden()


class TestWatermarkMenuComposition:
    """The watermark slot's menu, per the spec's markup: four corners, an
    opacity slider, and a note that the mark itself is Settings'."""

    def test_its_width_is_the_whole_menu_border_included(self):
        menu = overlay_module.WatermarkMenu()
        menu.resize(menu.sizeHint())

        assert menu.grab().deviceIndependentSize().width() == tokens.WatermarkMetric.MENU_W == 238

    def test_it_offers_the_four_corners_with_bottom_right_chosen(self):
        menu = overlay_module.WatermarkMenu()

        assert list(menu._corner_buttons) == ["tl", "tr", "bl", "br"]
        assert [button.toolTip() for button in menu._corner_buttons.values()] == [
            "Top left", "Top right", "Bottom left", "Bottom right"
        ]
        assert menu.corner == "br"
        assert [c for c, button in menu._corner_buttons.items() if button.is_selected] == ["br"]

    def test_opacity_runs_from_twenty_to_a_hundred_and_starts_at_seventy(self):
        menu = overlay_module.WatermarkMenu()

        assert (menu._slider.minimum(), menu._slider.maximum()) == (20, 100)
        assert menu.opacity == 70
        assert menu._readout.text() == "70%"

    def test_picking_a_corner_reports_it_and_the_menu_stays_open(self):
        menu = overlay_module.WatermarkMenu()
        menu.show()
        picked = Mock()
        menu.cornerPicked.connect(picked)

        QTest.mouseClick(menu._corner_buttons["tl"], Qt.MouseButton.LeftButton)

        picked.assert_called_once_with("tl")
        assert menu.corner == "tl"
        assert menu.isVisible()

    def test_moving_the_slider_reports_the_opacity_and_reads_it_out(self):
        menu = overlay_module.WatermarkMenu()
        changed = Mock()
        menu.opacityChanged.connect(changed)

        menu._slider.setValue(40)

        changed.assert_called_once_with(40)
        assert menu._readout.text() == "40%"

    def test_seeding_it_reports_nothing(self):
        menu = overlay_module.WatermarkMenu()
        picked, changed = Mock(), Mock()
        menu.cornerPicked.connect(picked)
        menu.opacityChanged.connect(changed)

        menu.set_corner("tr")
        menu.set_opacity(55)

        picked.assert_not_called()
        changed.assert_not_called()
        assert menu._readout.text() == "55%"

    def test_the_note_says_where_the_mark_itself_is_set(self):
        menu = overlay_module.WatermarkMenu()

        assert menu._note.text() == tokens.WATERMARK_MENU_NOTE
        assert "Settings" in menu._note.text()

    def test_its_wrapped_note_is_not_clipped(self):
        menu = overlay_module.WatermarkMenu()
        menu.resize(menu.sizeHint())
        menu.show()
        QTest.qWaitForWindowExposed(menu)

        note = menu._note
        assert note.height() >= note.heightForWidth(note.width())
        assert note.geometry().bottom() < menu.height()


class TestWatermarkOverlayIntegration:
    """The watermark slot and its menu, wired into `OverlayWindow`."""

    SELECTION = QRect(400, 200, 400, 300)

    def _overlay(self, selection=None) -> OverlayWindow:
        frame = make_frame(image_size=(1600, 1000), logical_size=(1600, 1000))
        overlay = OverlayWindow(frame)
        overlay.show()
        QTest.qWaitForWindowExposed(overlay)
        overlay.set_selection(selection or self.SELECTION)
        return overlay

    @staticmethod
    def _notch(overlay):
        QTest.mouseClick(overlay._bar._watermark.notch, Qt.MouseButton.LeftButton)
        return overlay._watermark_menu

    def test_with_nothing_in_settings_the_slot_is_greyed_with_why(self):
        overlay = self._overlay()
        slot = overlay._bar._watermark

        QTest.mouseClick(slot, Qt.MouseButton.LeftButton)
        QTest.mouseClick(slot.notch, Qt.MouseButton.LeftButton)

        assert slot.isVisible()
        assert slot.toolTip() == tokens.WATERMARK_UNSET
        assert not overlay_module.watermark_session.enabled
        assert not overlay._watermark_menu.isVisible()
        assert overlay._active_watermark() is None

    def test_image_chosen_with_none_kept_is_greyed_as_unset(self):
        setup_desktop.save_watermark_kind("image")
        setup_desktop.save_watermark_text("acme")

        overlay = self._overlay()

        assert overlay._bar._watermark.toolTip() == tokens.WATERMARK_UNSET

    def test_a_kept_image_that_went_missing_greys_it_with_that_reason(self, tmp_path):
        _keep_watermark_image(tmp_path)
        setup_desktop.load_watermark_image().unlink()

        overlay = self._overlay()

        assert overlay._bar._watermark.toolTip() == tokens.WATERMARK_IMAGE_MISSING

    def test_a_kept_image_that_cannot_be_read_greys_it_with_that_reason(self, tmp_path):
        _keep_watermark_image(tmp_path, data=b"not a picture at all")

        overlay = self._overlay()

        assert overlay._bar._watermark.toolTip() == tokens.WATERMARK_IMAGE_UNREADABLE

    def test_a_greyed_slot_never_stops_the_capture(self, tmp_path):
        _keep_watermark_image(tmp_path, data=b"not a picture at all")
        _switch_watermark_on()
        overlay = self._overlay()

        exported = overlay.rendered_image()

        assert exported.size() == self.SELECTION.size()

    def test_text_in_settings_makes_it_live(self):
        setup_desktop.save_watermark_text("acme · internal")
        overlay = self._overlay()

        QTest.mouseClick(overlay._bar._watermark, Qt.MouseButton.LeftButton)

        assert overlay._bar._watermark.is_on
        assert overlay._bar._watermark.toolTip() == tokens.WATERMARK_TOOLTIP_ON
        assert overlay._active_watermark().text == "acme · internal"

    def test_pressing_it_again_switches_it_off(self):
        setup_desktop.save_watermark_text("acme")
        overlay = self._overlay()
        QTest.mouseClick(overlay._bar._watermark, Qt.MouseButton.LeftButton)

        QTest.mouseClick(overlay._bar._watermark, Qt.MouseButton.LeftButton)

        assert not overlay._bar._watermark.is_on
        assert overlay._active_watermark() is None

    def test_the_notch_opens_its_menu_and_again_closes_it(self):
        setup_desktop.save_watermark_text("acme")
        overlay = self._overlay()

        assert self._notch(overlay).isVisible()
        assert not self._notch(overlay).isVisible()

    def test_it_opens_above_the_bar_against_its_slots_right_edge(self):
        setup_desktop.save_watermark_text("acme")
        overlay = self._overlay()

        menu = self._notch(overlay)

        bar = QRectF(overlay._bar.geometry())
        slot = QRectF(overlay._bar.watermark_rect(overlay))
        geometry = QRectF(menu.geometry())
        assert geometry.bottom() == bar.top() - tokens.BarMetric.MENU_OFFSET
        assert geometry.right() == pytest.approx(
            slot.right() + tokens.WatermarkMetric.MENU_OVERHANG, abs=1
        )

    def test_it_opens_below_the_bar_when_there_is_no_room_above(self):
        setup_desktop.save_watermark_text("acme")
        overlay = self._overlay(selection=QRect(400, 10, 400, 40))

        menu = self._notch(overlay)

        bar = QRectF(overlay._bar.geometry())
        assert QRectF(menu.geometry()).top() == bar.bottom() + tokens.BarMetric.MENU_OFFSET
        assert overlay._chrome_bounds().contains(QRectF(menu.geometry()))

    def test_one_menu_is_open_at_a_time(self):
        setup_desktop.save_watermark_text("acme")
        overlay = self._overlay()
        menu = self._notch(overlay)

        QTest.mouseClick(overlay._bar._tool_buttons["shapes"].notch, Qt.MouseButton.LeftButton)
        assert overlay._family_menus["shapes"].isVisible()
        assert not menu.isVisible()

        self._notch(overlay)
        assert menu.isVisible()
        assert not overlay._family_menus["shapes"].isVisible()

    def test_opening_it_closes_the_style_popover(self):
        setup_desktop.save_watermark_text("acme")
        overlay = self._overlay()
        QTest.mouseClick(overlay._bar._tool_buttons["pen"], Qt.MouseButton.LeftButton)
        QTest.mouseClick(overlay._bar._style_dot, Qt.MouseButton.LeftButton)
        assert overlay._style_popover.isVisible()

        menu = self._notch(overlay)

        assert menu.isVisible()
        assert not overlay._style_popover.isVisible()

    def test_opening_the_style_popover_closes_it(self):
        setup_desktop.save_watermark_text("acme")
        overlay = self._overlay()
        QTest.mouseClick(overlay._bar._tool_buttons["pen"], Qt.MouseButton.LeftButton)
        menu = self._notch(overlay)

        QTest.mouseClick(overlay._bar._style_dot, Qt.MouseButton.LeftButton)

        assert overlay._style_popover.isVisible()
        assert not menu.isVisible()

    def test_switching_it_closes_an_open_menu(self):
        setup_desktop.save_watermark_text("acme")
        overlay = self._overlay()
        QTest.mouseClick(overlay._bar._tool_buttons["redact"].notch, Qt.MouseButton.LeftButton)

        QTest.mouseClick(overlay._bar._watermark, Qt.MouseButton.LeftButton)

        assert not overlay._family_menus["redact"].isVisible()

    def test_a_press_outside_closes_it_and_does_nothing_else(self):
        setup_desktop.save_watermark_text("acme")
        overlay = self._overlay()
        menu = self._notch(overlay)

        QTest.mouseClick(overlay, Qt.MouseButton.LeftButton, pos=QPoint(2, 2))

        assert not menu.isVisible()
        assert overlay._selection == self.SELECTION

    def test_a_press_on_it_stays_in_it(self):
        setup_desktop.save_watermark_text("acme")
        overlay = self._overlay()
        menu = self._notch(overlay)

        QTest.mouseClick(menu, Qt.MouseButton.LeftButton, pos=QPoint(4, 4))

        assert menu.isVisible()
        assert overlay._selection == self.SELECTION

    def test_esc_closes_it_and_nothing_else(self):
        setup_desktop.save_watermark_text("acme")
        overlay = self._overlay()
        overlay.add_mark(
            Rectangle(
                colour=QColor(255, 0, 0), stroke_width=4,
                start=QPointF(450, 250), end=QPointF(500, 300),
            )
        )
        menu = self._notch(overlay)

        QTest.keyClick(overlay, Qt.Key.Key_Escape)

        assert not menu.isVisible()
        assert len(overlay.marks) == 1
        assert overlay._selection == self.SELECTION

    def test_its_corner_and_opacity_move_the_mark(self):
        setup_desktop.save_watermark_text("acme")
        overlay = self._overlay()
        QTest.mouseClick(overlay._bar._watermark, Qt.MouseButton.LeftButton)
        menu = self._notch(overlay)

        QTest.mouseClick(menu._corner_buttons["tl"], Qt.MouseButton.LeftButton)
        menu._slider.setValue(35)

        mark = overlay._active_watermark()
        assert (mark.corner, mark.opacity) == ("tl", 35)
        assert menu.isVisible()

    def test_it_hides_with_the_overlay_and_with_the_selection(self):
        setup_desktop.save_watermark_text("acme")
        overlay = self._overlay()
        menu = self._notch(overlay)

        overlay.set_selection(None)
        assert not menu.isVisible()

        overlay.set_selection(self.SELECTION)
        self._notch(overlay)
        overlay.hide()
        assert not menu.isVisible()


class TestWatermarkPreview:
    """While it is on, the mark previews inside the selection, at the corner
    and opacity the menu set."""

    SELECTION = QRect(400, 200, 400, 300)

    def _grab(self, tmp_path, corner: str) -> tuple[QImage, float]:
        _keep_watermark_image(tmp_path)
        _switch_watermark_on(corner)
        frame = make_frame(image_size=(1600, 1000), logical_size=(1600, 1000))
        # Never shown, so no chrome is laid over what is sampled.
        overlay = OverlayWindow(frame)
        overlay.set_selection(self.SELECTION)
        image = overlay.grab().toImage()
        return image, image.devicePixelRatio()

    @pytest.mark.parametrize(
        "corner,left,top",
        [
            # A 400x300 selection: 5% of its shorter side is under the floor,
            # so the 2:1 image is a 40x20 mark, 14 in from its corner.
            ("tl", 400 + 14, 200 + 14),
            ("tr", 800 - 14 - 40, 200 + 14),
            ("bl", 400 + 14, 500 - 14 - 20),
            ("br", 800 - 14 - 40, 500 - 14 - 20),
        ],
    )
    def test_it_previews_in_the_selections_corner(self, tmp_path, corner, left, top):
        image, ratio = self._grab(tmp_path, corner)

        found = _exact_bounds(image, WATERMARK_MAGENTA, _physical(QRectF(self.SELECTION), ratio))

        _assert_edges_near(found, _physical(QRectF(left, top, 40, 20), ratio))

    def test_it_previews_at_its_opacity(self, tmp_path):
        _keep_watermark_image(tmp_path)
        _switch_watermark_on("br", opacity=50)
        frame = make_frame(image_size=(1600, 1000), logical_size=(1600, 1000))
        overlay = OverlayWindow(frame)
        overlay.set_selection(self.SELECTION)

        colour = pixel(overlay.grab().toImage(), 800 - 14 - 20, 500 - 14 - 10)

        assert colour.red() == pytest.approx((10 + 255) / 2, abs=3)
        assert colour.green() == pytest.approx(20 / 2, abs=3)

    def test_nothing_previews_while_it_is_off(self, tmp_path):
        _keep_watermark_image(tmp_path)
        frame = make_frame(image_size=(1600, 1000), logical_size=(1600, 1000))
        overlay = OverlayWindow(frame)
        overlay.set_selection(self.SELECTION)

        assert _exact_bounds(overlay.grab().toImage(), WATERMARK_MAGENTA).isNull()

    def test_nothing_previews_while_a_recording_is_framed(self, tmp_path):
        _keep_watermark_image(tmp_path)
        _switch_watermark_on()
        frame = make_frame(image_size=(1600, 1000), logical_size=(1600, 1000))
        overlay = OverlayWindow(frame)
        overlay._chooser.set_kind("record")
        overlay.set_selection(self.SELECTION)

        assert _exact_bounds(overlay.grab().toImage(), WATERMARK_MAGENTA).isNull()


class TestWatermarkExport:
    """The export carries the mark exactly where the preview put it.

    Runs at whatever scale the suite does -- it is kept green at
    QT_SCALE_FACTOR=1 and 1.5 -- over a frame the size a capture at that
    scale really is, as `TestExportedStrokeWidth` does. The inset and the
    mark's size are logical pixels; the preview is read in the window's
    physical pixels and the export in the crop's, and both must show the
    same mark: not one two-thirds the size of the other.
    """

    LOGICAL = (400, 300)
    SELECTION = QRect(40, 60, 320, 200)

    def _overlay(self, tmp_path, corner: str) -> tuple[OverlayWindow, float]:
        ratio = QGuiApplication.primaryScreen().devicePixelRatio()
        _keep_watermark_image(tmp_path)
        _switch_watermark_on(corner)
        width, height = self.LOGICAL
        frame = make_frame(
            image_size=(round(width * ratio), round(height * ratio)),
            logical_size=self.LOGICAL,
        )
        overlay = OverlayWindow(frame)
        overlay.set_selection(self.SELECTION)
        return overlay, ratio

    @pytest.mark.parametrize(
        "corner,left,top",
        [
            # A 320x200 selection gives a 40x20 mark, 14 in.
            ("tl", 40 + 14, 60 + 14),
            ("tr", 360 - 14 - 40, 60 + 14),
            ("bl", 40 + 14, 260 - 14 - 20),
            ("br", 360 - 14 - 40, 260 - 14 - 20),
        ],
    )
    def test_the_export_carries_the_mark_where_the_preview_put_it(self, tmp_path, corner, left, top):
        overlay, ratio = self._overlay(tmp_path, corner)
        logical = QRectF(left, top, 40, 20)

        on_screen = overlay.grab().toImage()
        exported = overlay.rendered_image()

        seen = _exact_bounds(on_screen, WATERMARK_MAGENTA)
        saved = _exact_bounds(exported, WATERMARK_MAGENTA)
        assert on_screen.devicePixelRatio() == ratio  # the grab really is physical
        _assert_edges_near(seen, _physical(logical, ratio))
        _assert_edges_near(saved, _physical(logical, ratio, QPointF(self.SELECTION.topLeft())))
        assert abs(saved.width() - seen.width()) <= 1
        assert abs(saved.height() - seen.height()) <= 1

    def test_nothing_is_stamped_while_it_is_off(self, tmp_path):
        overlay, _ratio = self._overlay(tmp_path, "br")
        overlay_module.watermark_session.enabled = False

        assert _exact_bounds(overlay.rendered_image(), WATERMARK_MAGENTA).isNull()


class TestWatermarkTextStyleInOverlay:
    """The colour, font and plate Settings keeps reach the mark the preview
    shows and the one the export stamps, read at the start of the snip."""

    SELECTION = QRect(40, 60, 320, 200)

    def _overlay(self) -> OverlayWindow:
        setup_desktop.save_watermark_kind("text")
        setup_desktop.save_watermark_text("ACME")
        setup_desktop.save_watermark_color("#ff00ff")
        setup_desktop.save_watermark_font("Georgia")
        setup_desktop.save_watermark_backing(False)
        _switch_watermark_on("br")
        overlay = OverlayWindow(make_frame(image_size=(400, 300), logical_size=(400, 300)))
        overlay.set_selection(self.SELECTION)
        return overlay

    def test_the_mark_carries_the_kept_style(self):
        mark = self._overlay()._active_watermark()

        assert mark.color == QColor("#ff00ff")
        assert mark.font_family == "Georgia"
        assert mark.backing is False

    def test_the_export_is_stamped_in_that_colour(self):
        exported = self._overlay().rendered_image()

        magentaish = sum(
            1
            for x in range(exported.width())
            for y in range(exported.height())
            if (c := exported.pixelColor(x, y)).red() > 200 and c.blue() > 200 and c.green() < 80
        )
        assert magentaish > 10

    def test_a_change_in_settings_reaches_the_next_snip(self):
        self._overlay()
        setup_desktop.save_watermark_color("#00ff00")

        after = OverlayWindow(make_frame(image_size=(400, 300), logical_size=(400, 300)))
        after.set_selection(self.SELECTION)

        assert after._active_watermark().color == QColor("#00ff00")


class TestWatermarkIsNotAMark:
    """Applied once, on export, above every annotation -- never a mark in the
    undo stack, and out of the eraser's reach."""

    SELECTION = QRect(0, 0, 400, 300)
    MARK_CENTRE = QPointF(366, 276)  # of a 40x20 mark in a 400x300 selection's bottom right

    def _overlay(self, tmp_path) -> OverlayWindow:
        _keep_watermark_image(tmp_path)
        _switch_watermark_on("br")
        overlay = OverlayWindow(make_frame(image_size=(400, 300), logical_size=(400, 300)))
        overlay.set_selection(self.SELECTION)
        return overlay

    def _stamped(self, overlay) -> bool:
        return overlay.rendered_image().pixelColor(366, 276) == WATERMARK_MAGENTA

    def test_switching_it_on_leaves_nothing_to_undo(self):
        setup_desktop.save_watermark_text("acme")
        overlay = OverlayWindow(make_frame(image_size=(1600, 1000), logical_size=(1600, 1000)))
        overlay.show()
        QTest.qWaitForWindowExposed(overlay)
        overlay.set_selection(QRect(400, 200, 400, 300))

        QTest.mouseClick(overlay._bar._watermark, Qt.MouseButton.LeftButton)

        assert overlay._bar._watermark.is_on
        assert overlay.marks == ()
        assert not overlay.can_undo
        assert not overlay._bar._undo_button.isEnabled()

    def test_undo_and_clear_leave_it_on_the_export(self, tmp_path):
        overlay = self._overlay(tmp_path)
        line = Line(
            colour=QColor(0, 0, 255), stroke_width=6,
            start=QPointF(20, 20), end=QPointF(200, 200),
        )

        overlay.add_mark(line)
        overlay.undo()
        assert self._stamped(overlay)
        assert not overlay.can_undo

        overlay.add_mark(line)
        overlay.clear()
        assert self._stamped(overlay)
        overlay.undo()
        assert overlay.marks == (line,)
        assert self._stamped(overlay)

    def test_it_is_stamped_above_every_annotation(self, tmp_path):
        overlay = self._overlay(tmp_path)
        blue = QColor(0, 0, 255)

        overlay.add_mark(
            Line(colour=blue, stroke_width=60, start=QPointF(300, 276), end=QPointF(400, 276))
        )

        exported = overlay.rendered_image()
        assert exported.pixelColor(366, 276) == WATERMARK_MAGENTA
        assert exported.pixelColor(320, 276) == blue

    def test_the_eraser_cannot_take_it(self, tmp_path):
        overlay = self._overlay(tmp_path)

        assert overlay.erase_at(self.MARK_CENTRE) is None

        assert overlay.marks == ()
        assert self._stamped(overlay)


class TestWatermarkCarriesAcrossSnips:
    """The toggle, corner and opacity last the session; the content comes
    from Settings, fresh for every snip."""

    SELECTION = QRect(400, 200, 400, 300)

    def _overlay(self) -> OverlayWindow:
        overlay = OverlayWindow(make_frame(image_size=(1600, 1000), logical_size=(1600, 1000)))
        overlay.show()
        QTest.qWaitForWindowExposed(overlay)
        overlay.set_selection(self.SELECTION)
        return overlay

    def test_a_fresh_session_starts_off_in_the_bottom_right_at_seventy(self):
        choice = overlay_module.WatermarkChoice()

        assert (choice.enabled, choice.corner, choice.opacity) == (False, "br", 70)

    def test_on_corner_and_opacity_carry_to_the_next_snip(self):
        setup_desktop.save_watermark_text("acme")
        first = self._overlay()
        QTest.mouseClick(first._bar._watermark, Qt.MouseButton.LeftButton)
        QTest.mouseClick(first._bar._watermark.notch, Qt.MouseButton.LeftButton)
        QTest.mouseClick(first._watermark_menu._corner_buttons["tl"], Qt.MouseButton.LeftButton)
        first._watermark_menu._slider.setValue(40)
        first.close()

        second = self._overlay()

        assert second._bar._watermark.is_on
        mark = second._active_watermark()
        assert (mark.corner, mark.opacity) == ("tl", 40)
        assert (second._watermark_menu.corner, second._watermark_menu.opacity) == ("tl", 40)

    def test_the_content_is_read_fresh_for_each_snip(self):
        setup_desktop.save_watermark_text("first")
        _switch_watermark_on()
        first = self._overlay()
        first.close()

        setup_desktop.save_watermark_text("second")
        second = self._overlay()

        assert second._active_watermark().text == "second"

    def test_content_removed_in_settings_greys_the_next_snip_without_losing_the_toggle(self):
        setup_desktop.save_watermark_text("acme")
        _switch_watermark_on()
        setup_desktop.save_watermark_text("")

        overlay = self._overlay()

        assert overlay._bar._watermark.toolTip() == tokens.WATERMARK_UNSET
        assert not overlay._bar._watermark.is_on
        assert overlay_module.watermark_session.enabled


class TestWatermarkMenuStaysClickable:
    """#74's rule holds for the watermark menu: the tool hint strip gives way
    to it. On the strip's side of the bar the strip would otherwise sit over
    a corner or the slider and take the click meant for it. Driven through
    the window, hover first, the way `TestFamilyMenuRowsStayClickable` drives
    the family menus.
    """

    @pytest.fixture(autouse=True)
    def _clean_slate(self):
        _close_stray_toplevel_windows()

    # The same pointer, the same way: a hover-only move through the window's
    # own handle, then a click at the same point.
    _move_to = staticmethod(TestFamilyMenuRowsStayClickable._move_to)
    _click = TestFamilyMenuRowsStayClickable._click

    def _overlay(self, placement):
        setup_desktop.save_watermark_text("acme")
        # The whole offscreen screen, so every point is on a real widget at
        # any scale factor.
        screen = QGuiApplication.primaryScreen().geometry()
        width, height = screen.width(), screen.height()
        overlay = OverlayWindow(make_frame(image_size=(width, height), logical_size=(width, height)))
        overlay.setGeometry(0, 0, width, height)
        overlay.show()
        QTest.qWaitForWindowExposed(overlay)
        margin = height // 10
        if placement == "below":
            overlay.set_selection(QRect(margin, margin, width - 2 * margin, height // 3))
        else:
            top = height // 3
            overlay.set_selection(QRect(margin, top, width - 2 * margin, height - top - 4))
        QApplication.processEvents()
        bar, selection = overlay._bar.geometry(), overlay._selection
        if placement == "below":
            assert bar.top() > selection.bottom()
        else:
            assert bar.bottom() < selection.top()
        return overlay

    def _open(self, overlay):
        bar = overlay._bar
        # Hovering a slot is what brings the strip up in the first place.
        self._move_to(overlay, bar._tool_buttons["pen"])
        self._move_to(overlay, bar._watermark)
        self._click(overlay, bar._watermark.notch)
        menu = overlay._watermark_menu
        assert not menu.isHidden()
        return menu

    @pytest.mark.parametrize("placement", ["below", "above"])
    def test_every_control_is_what_the_pointer_finds_after_hovering_the_slot(self, placement):
        overlay = self._overlay(placement)
        menu = self._open(overlay)

        assert overlay._tool_hint.isHidden()
        for control in (*menu._corner_buttons.values(), menu._slider):
            hit = overlay.childAt(control.mapTo(overlay, control.rect().center()))
            assert hit is control or control.isAncestorOf(hit), (
                f"{control!r} is under {type(hit).__name__}"
            )

    @pytest.mark.parametrize("placement", ["below", "above"])
    def test_each_corner_can_be_picked_through_the_window(self, placement):
        overlay = self._overlay(placement)
        menu = self._open(overlay)

        for corner in ("tl", "tr", "bl", "br"):
            self._click(overlay, menu._corner_buttons[corner])

            assert overlay_module.watermark_session.corner == corner
            assert not menu.isHidden()
            assert overlay._tool_hint.isHidden()

    def test_hovering_the_slots_with_it_open_keeps_the_hint_away(self):
        overlay = self._overlay("above")
        bar = overlay._bar
        self._open(overlay)

        for slot in (*bar._tool_buttons.values(), bar._style_dot, bar._watermark):
            self._move_to(overlay, slot)
            assert overlay._tool_hint.isHidden()

    def test_closing_it_brings_the_hint_back(self):
        overlay = self._overlay("above")
        self._open(overlay)

        self._click(overlay, overlay._bar._watermark.notch)

        assert overlay._watermark_menu.isHidden()
        assert overlay._tool_hint.isVisible()

    @pytest.mark.parametrize("where", ["top-left", "bottom-right"])
    def test_it_and_the_style_popover_close_each_other_and_fit_the_monitor(self, where):
        # Against the roomier style popover (#75): a small selection in a
        # corner pushes the bar against two monitor edges, where each has the
        # least room to open into.
        setup_desktop.save_watermark_text("acme")
        screen = QGuiApplication.primaryScreen().geometry()
        width, height = screen.width(), screen.height()
        overlay = OverlayWindow(make_frame(image_size=(width, height), logical_size=(width, height)))
        overlay.setGeometry(0, 0, width, height)
        overlay.show()
        QTest.qWaitForWindowExposed(overlay)
        if where == "top-left":
            overlay.set_selection(QRect(4, 4, 120, 60))
        else:
            overlay.set_selection(QRect(width - 124, height - 64, 120, 60))
        QApplication.processEvents()
        bar = overlay._bar
        menu, popover = overlay._watermark_menu, overlay._style_popover
        bounds = overlay._bar_bounds()
        assert bar.active_tool == "pen"

        self._click(overlay, bar._style_dot)
        assert not popover.isHidden()
        assert bounds.contains(QRectF(popover.geometry())), (popover.geometry(), bounds)

        self._click(overlay, bar._watermark.notch)
        assert popover.isHidden()
        assert not menu.isHidden()
        assert bounds.contains(QRectF(menu.geometry())), (menu.geometry(), bounds)

        self._click(overlay, bar._style_dot)
        assert menu.isHidden()
        assert not popover.isHidden()
        assert bounds.contains(QRectF(popover.geometry())), (popover.geometry(), bounds)


class TestTheHighlighterSnapsToText:
    """A highlighter sweep over text commits as bands fitted to the lines
    (`snipux.textsnap`), read off the frozen frame itself; the style popover's
    snap button switches the tool back to freehand."""

    @staticmethod
    def _text_overlay(scale: int = 1) -> OverlayWindow:
        image = QImage(400 * scale, 120 * scale, QImage.Format.Format_RGB32)
        image.fill(QColor("#ffffff"))
        painter = QPainter(image)
        painter.scale(scale, scale)
        font = QFont()
        font.setPixelSize(18)
        painter.setFont(font)
        painter.setPen(QColor("#202020"))
        painter.drawText(QPointF(20, 40), "several words of text")
        painter.end()
        frame = Frame(image=image, logical_origin=QPointF(0, 0), logical_size=QSizeF(400, 120))
        overlay = OverlayWindow(frame)
        overlay.setGeometry(0, 0, 400, 120)
        overlay.set_selection(QRect(0, 0, 400, 120))
        overlay._bar.select_tool("highlighter")
        return overlay

    @staticmethod
    def _sweep(overlay: OverlayWindow) -> None:
        QTest.mousePress(overlay, Qt.MouseButton.LeftButton, pos=QPoint(40, 38))
        QTest.mouseMove(overlay, QPoint(90, 31))
        QTest.mouseMove(overlay, QPoint(140, 37))
        QTest.mouseRelease(overlay, Qt.MouseButton.LeftButton, pos=QPoint(140, 37))

    def test_a_sweep_over_text_commits_as_a_band_on_the_line(self):
        overlay = self._text_overlay()

        self._sweep(overlay)

        (mark,) = overlay.marks
        assert isinstance(mark, Highlighter)
        (band,) = mark.bands
        # On the line of text, which sits between y=26 and y=45 or so --
        # not wherever the wandering sweep happened to go.
        assert 20 <= band.top() <= 30 and 40 <= band.bottom() <= 50
        assert band.left() <= 22

    def test_the_band_is_in_window_coordinates_on_a_scaled_frame(self):
        # The frame carries twice the pixels; the mark must still be where
        # the text is on screen, not twice as far along.
        at_one = self._text_overlay()
        at_two = self._text_overlay(scale=2)

        self._sweep(at_one)
        self._sweep(at_two)

        (one,) = at_one.marks[0].bands
        (two,) = at_two.marks[0].bands
        assert abs(one.left() - two.left()) <= 2 and abs(one.top() - two.top()) <= 2
        assert abs(one.right() - two.right()) <= 2 and abs(one.bottom() - two.bottom()) <= 2

    def test_freehand_keeps_the_sweep_as_drawn(self):
        overlay = self._text_overlay()
        overlay._styles.update("highlighter", snap="free")

        self._sweep(overlay)

        (mark,) = overlay.marks
        assert mark.bands == []
        assert len(mark.points) >= 3

    def test_the_popover_snap_button_switches_the_tool_to_freehand_and_back(self):
        styles = ToolStyles()
        popover = StylePopover(styles)
        popover.set_tool("highlighter")

        QTest.mouseClick(popover._snap_button, Qt.MouseButton.LeftButton)
        freehand = styles.of("highlighter").snap
        QTest.mouseClick(popover._snap_button, Qt.MouseButton.LeftButton)

        assert (freehand, styles.of("highlighter").snap) == ("free", "text")
        assert popover._snap_button.toolTip() == "Snap to text → click for Freehand"

    def test_only_the_highlighter_offers_the_snap_button(self):
        popover = StylePopover(ToolStyles())

        popover.set_tool("pen")

        assert "snap" not in popover.sections()
