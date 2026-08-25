import os
import subprocess
from unittest.mock import Mock

from PyQt6.QtCore import QPointF, QRect, QRectF, QSizeF
from PyQt6.QtGui import QColor, QImage, QPixmap, qRgb
from PyQt6.QtWidgets import QApplication

import pytest

import snipux.capture as capture
from snipux.capture import (
    BackendRegistry,
    CaptureBackend,
    CaptureError,
    Frame,
    detect_session_type,
)


@pytest.fixture(scope="module", autouse=True)
def qapp():
    # QPixmap/QPainter (used by the Qt-native X11 backend and its test
    # doubles below) crash without a live QGuiApplication, even offscreen —
    # unlike plain QImage, which the rest of this file uses fine without one.
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


class _FakeScreen:
    """Stand-in for QScreen exposing only what capture.py's X11 backends
    call: geometry(), devicePixelRatio(), and grabWindow().
    """

    def __init__(self, geometry: QRect, ratio: float = 1.0):
        self._geometry = geometry
        self._ratio = ratio
        self._pixmap = None

    def geometry(self) -> QRect:
        return self._geometry

    def devicePixelRatio(self) -> float:
        return self._ratio

    def grabWindow(self, window_id):
        # Built lazily (not in __init__) so a plain _FakeScreen used only
        # for geometry math (e.g. TestVirtualDesktopGeometry) never touches
        # QPixmap at all.
        if self._pixmap is None:
            self._pixmap = QPixmap(self._geometry.width(), self._geometry.height())
            self._pixmap.fill(QColor(9, 9, 9))
        return self._pixmap


class _FakeQGuiApplication:
    """Stand-in for the QGuiApplication class object itself.

    capture.py calls `QGuiApplication.screens()` / `.primaryScreen()`
    class-style, without instantiating; monkeypatching the module's
    `QGuiApplication` name to *an instance* of this class works the same
    way, since attribute lookup finds these bound methods either way.
    """

    def __init__(self, screens: list[_FakeScreen]):
        self._screens = screens

    def screens(self):
        return self._screens

    def primaryScreen(self):
        return self._screens[0]


def _placeholder_png_writer(color=(1, 2, 3)):
    """A `subprocess.run` replacement for the shell-out backends: instead
    of actually running a binary, writes a tiny real PNG to the path the
    backend passed so the backend's own `QImage(path)` load succeeds.
    """

    def fake_run(argv, **kwargs):
        path = argv[-1]
        image = QImage(2, 2, QImage.Format.Format_RGB32)
        image.fill(qRgb(*color))
        image.save(path, "PNG")
        return Mock(returncode=0)

    return fake_run


def _write_placeholder_png(path, color=(4, 5, 6)):
    """Writes a tiny real PNG straight to `path` — the Wayland D-Bus-based
    backends don't shell out to anything `_placeholder_png_writer` could
    intercept, so their tests need a file already sitting at the path/URI
    the fake D-Bus reply points at.
    """
    image = QImage(2, 2, QImage.Format.Format_RGB32)
    image.fill(qRgb(*color))
    image.save(path, "PNG")


def _set_session_type(monkeypatch, session_type):
    """Sets or unsets XDG_SESSION_TYPE, matching detect_session_type()'s
    two ways of *not* being 'wayland': the env var set to something else,
    or not set at all.
    """
    if session_type is None:
        monkeypatch.delenv("XDG_SESSION_TYPE", raising=False)
    else:
        monkeypatch.setenv("XDG_SESSION_TYPE", session_type)


def make_frame(
    image_size=(200, 100), logical_size=(200, 100), logical_origin=(0, 0)
) -> Frame:
    image = QImage(*image_size, QImage.Format.Format_RGB32)
    image.fill(qRgb(10, 20, 30))
    return Frame(
        image=image,
        logical_origin=QPointF(*logical_origin),
        logical_size=QSizeF(*logical_size),
    )


class TestFrameCrop:
    def test_crop_returns_a_frame_matching_the_requested_rect(self):
        frame = make_frame()
        rect = QRectF(10, 20, 30, 40)

        cropped = frame.crop(rect)

        assert isinstance(cropped, Frame)
        assert cropped.logical_origin == QPointF(rect.x(), rect.y())
        assert cropped.logical_size == QSizeF(rect.width(), rect.height())

    def test_crop_scales_independently_on_each_axis(self):
        # image is 2x logical width and 3x logical height: a uniform-scalar
        # implementation would get one of these two axes wrong.
        frame = make_frame(image_size=(400, 300), logical_size=(200, 100))
        rect = QRectF(10, 10, 20, 20)

        cropped = frame.crop(rect)

        assert cropped.image.width() == 40  # 20 * scale_x (2)
        assert cropped.image.height() == 60  # 20 * scale_y (3)

    def test_crop_on_virtual_desktop_with_negative_origin(self):
        # A monitor positioned above/left of the primary produces a virtual
        # desktop origin like (-1920, 0). A logical rect expressed in
        # absolute virtual-desktop coordinates must still crop the correct
        # image-local pixels.
        image = QImage(200, 100, QImage.Format.Format_RGB32)
        image.fill(qRgb(0, 0, 0))
        # Paint a distinctive block at image-local (10, 10)-(30, 30) so we
        # can confirm the crop grabbed the right pixels, not just the right
        # size.
        for x in range(10, 30):
            for y in range(10, 30):
                image.setPixelColor(x, y, qRgb(255, 0, 0))

        frame = Frame(
            image=image,
            logical_origin=QPointF(-1920, 0),
            logical_size=QSizeF(200, 100),
        )
        # Absolute logical rect: origin (-1920,0) + local (10,10) = (-1910, 10)
        rect = QRectF(-1910, 10, 20, 20)

        cropped = frame.crop(rect)

        assert cropped.image.width() == 20
        assert cropped.image.height() == 20
        assert cropped.image.pixelColor(0, 0) == image.pixelColor(10, 10)
        assert cropped.image.pixelColor(0, 0).red() == 255

    def test_crop_tiles_exactly_under_fractional_scaling(self):
        # At a fractional scale (1.25x, GNOME's common case) rounding a
        # rect's width independently of its x can put its right edge a
        # pixel away from where an adjacent rect's left edge rounds to.
        # Cropping two side-by-side rects must produce pixel widths that
        # sum to exactly the width of cropping their union — i.e. the
        # crops tile with no gap and no overlap.
        frame = make_frame(image_size=(125, 125), logical_size=(100, 100))
        left_rect = QRectF(3, 0, 7, 10)
        right_rect = QRectF(10, 0, 10, 10)
        combined_rect = QRectF(3, 0, 17, 10)

        left_crop = frame.crop(left_rect)
        right_crop = frame.crop(right_rect)
        combined_crop = frame.crop(combined_rect)

        assert (
            left_crop.image.width() + right_crop.image.width()
            == combined_crop.image.width()
        )


class FakeBackend(CaptureBackend):
    def __init__(self, backend_name, available, reason=None, capture_result=None, capture_error=None):
        self._name = backend_name
        self._available = available
        self._reason = reason
        self._capture_result = capture_result
        self._capture_error = capture_error

    def name(self):
        return self._name

    def is_available(self):
        return self._available

    def unavailable_reason(self):
        return self._reason

    def capture(self):
        if self._capture_error is not None:
            raise self._capture_error
        return self._capture_result


class TestBackendRegistry:
    def test_available_filters_to_available_backends_only(self):
        available_backend = FakeBackend("a", True)
        unavailable_backend = FakeBackend("b", False, reason="no display")
        registry = BackendRegistry([available_backend, unavailable_backend])

        assert registry.available() == [available_backend]

    def test_capture_skips_a_failing_backend_and_tries_the_next(self):
        good_frame = make_frame()
        failing = FakeBackend("failing", True, capture_error=RuntimeError("boom"))
        succeeding = FakeBackend("succeeding", True, capture_result=good_frame)
        registry = BackendRegistry([failing, succeeding])

        result = registry.capture()

        assert result is good_frame

    def test_capture_raises_with_all_failures_when_every_backend_fails(self):
        first_error = RuntimeError("first boom")
        second_error = RuntimeError("second boom")
        registry = BackendRegistry(
            [
                FakeBackend("first", True, capture_error=first_error),
                FakeBackend("second", True, capture_error=second_error),
            ]
        )

        with pytest.raises(CaptureError) as excinfo:
            registry.capture()

        failures = excinfo.value.failures
        assert failures == [("first", first_error), ("second", second_error)]
        assert str(excinfo.value) == "all capture backends failed: first: first boom; second: second boom"

    def test_capture_names_a_wayland_package_when_no_backend_is_available(self, monkeypatch):
        monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
        registry = BackendRegistry([FakeBackend("b", False, reason="not a Wayland session")])

        with pytest.raises(CaptureError) as excinfo:
            registry.capture()

        assert excinfo.value.failures == []
        message = str(excinfo.value)
        assert message.startswith("no capture backend is available:")
        assert not message.rstrip().endswith(":")
        assert "grim" in message

    def test_capture_names_an_x11_package_when_no_backend_is_available(self, monkeypatch):
        monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
        registry = BackendRegistry([FakeBackend("b", False, reason="not an X11 session")])

        with pytest.raises(CaptureError) as excinfo:
            registry.capture()

        assert excinfo.value.failures == []
        message = str(excinfo.value)
        assert message.startswith("no capture backend is available:")
        assert not message.rstrip().endswith(":")
        assert "maim" in message

    def test_capture_still_names_a_package_when_session_type_is_unknown(self, monkeypatch):
        monkeypatch.delenv("XDG_SESSION_TYPE", raising=False)
        registry = BackendRegistry([])

        with pytest.raises(CaptureError) as excinfo:
            registry.capture()

        message = str(excinfo.value)
        assert message.startswith("no capture backend is available:")
        assert not message.rstrip().endswith(":")
        assert "grim" in message and "maim" in message


class TestDetectSessionType:
    def test_wayland(self, monkeypatch):
        monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
        assert detect_session_type() == "wayland"

    def test_x11(self, monkeypatch):
        monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
        assert detect_session_type() == "x11"

    def test_unset(self, monkeypatch):
        monkeypatch.delenv("XDG_SESSION_TYPE", raising=False)
        assert detect_session_type() == "unknown"

    def test_other_value(self, monkeypatch):
        monkeypatch.setenv("XDG_SESSION_TYPE", "tty")
        assert detect_session_type() == "unknown"


class TestVirtualDesktopGeometry:
    def test_returns_union_of_screen_geometries(self, monkeypatch):
        screens = [
            _FakeScreen(QRect(0, 0, 800, 600)),
            _FakeScreen(QRect(800, 0, 1024, 768)),
        ]
        monkeypatch.setattr(capture, "QGuiApplication", _FakeQGuiApplication(screens))

        result = capture._virtual_desktop_geometry()

        assert result == QRectF(0, 0, 1824, 768)

    def test_qt_native_and_shell_out_backends_agree_on_the_geometry(self, monkeypatch):
        # Per PLAN.md review point 3: both backend families must derive
        # their logical origin/size from the same helper, not two
        # independently-written union loops that could drift apart.
        monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
        monkeypatch.setattr(capture.shutil, "which", lambda binary: f"/usr/bin/{binary}")
        screens = [_FakeScreen(QRect(-100, 0, 300, 200))]
        monkeypatch.setattr(capture, "QGuiApplication", _FakeQGuiApplication(screens))
        monkeypatch.setattr(capture.subprocess, "run", _placeholder_png_writer())

        qt_frame = capture.QtNativeX11Backend().capture()
        maim_frame = capture.MaimBackend().capture()

        assert qt_frame.logical_origin == maim_frame.logical_origin == QPointF(-100, 0)
        assert qt_frame.logical_size == maim_frame.logical_size == QSizeF(300, 200)


class TestQtNativeX11Backend:
    def test_is_available_only_under_x11(self, monkeypatch):
        backend = capture.QtNativeX11Backend()

        monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
        assert backend.is_available() is True

        monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
        assert backend.is_available() is False

    def test_unavailable_reason_matches_availability(self, monkeypatch):
        backend = capture.QtNativeX11Backend()

        monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
        assert backend.unavailable_reason() is None

        monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
        assert backend.unavailable_reason() == "not an X11 session"

    def test_capture_covers_the_union_of_all_screens(self, monkeypatch):
        monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
        screens = [
            _FakeScreen(QRect(0, 0, 100, 50)),
            _FakeScreen(QRect(100, 0, 80, 50)),
        ]
        monkeypatch.setattr(capture, "QGuiApplication", _FakeQGuiApplication(screens))

        frame = capture.QtNativeX11Backend().capture()

        assert frame.logical_origin == QPointF(0, 0)
        assert frame.logical_size == QSizeF(180, 50)
        assert frame.image.width() == 180
        assert frame.image.height() == 50
        assert not frame.image.isNull()

    def test_capture_uses_the_primary_screens_ratio_not_each_screens_own(self, monkeypatch):
        # Locks in the "one session-wide ratio" design from PLAN.md's
        # review point 1: the second screen's own 1x ratio must not leak
        # into compositing just because it differs from the primary's.
        monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
        screens = [
            _FakeScreen(QRect(0, 0, 100, 50), ratio=2.0),
            _FakeScreen(QRect(100, 0, 100, 50), ratio=1.0),
        ]
        monkeypatch.setattr(capture, "QGuiApplication", _FakeQGuiApplication(screens))

        frame = capture.QtNativeX11Backend().capture()

        # Virtual desktop is 200x50 logical; at the primary's 2x ratio the
        # composited image should be uniformly 400x100.
        assert frame.image.width() == 400
        assert frame.image.height() == 100


class TestShellOutX11Backends:
    BACKENDS = [
        (capture.MaimBackend, "maim"),
        (capture.ImportBackend, "import"),
        (capture.ScrotBackend, "scrot"),
    ]

    @pytest.mark.parametrize("backend_cls,binary", BACKENDS)
    def test_unavailable_when_binary_missing(self, monkeypatch, backend_cls, binary):
        monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
        monkeypatch.setattr(capture.shutil, "which", lambda b: None)

        backend = backend_cls()

        assert backend.is_available() is False
        assert binary in backend.unavailable_reason()

    @pytest.mark.parametrize("backend_cls,binary", BACKENDS)
    def test_unavailable_off_x11_even_with_binary_present(self, monkeypatch, backend_cls, binary):
        monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
        monkeypatch.setattr(capture.shutil, "which", lambda b: f"/usr/bin/{b}")

        backend = backend_cls()

        assert backend.is_available() is False
        assert backend.unavailable_reason() == "not an X11 session"

    @pytest.mark.parametrize("backend_cls,binary", BACKENDS)
    def test_available_when_x11_and_binary_present(self, monkeypatch, backend_cls, binary):
        monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
        monkeypatch.setattr(capture.shutil, "which", lambda b: f"/usr/bin/{b}")

        assert backend_cls().is_available() is True
        assert backend_cls().unavailable_reason() is None

    @pytest.mark.parametrize("backend_cls,binary", BACKENDS)
    def test_capture_invokes_the_expected_binary_and_returns_a_frame(
        self, monkeypatch, backend_cls, binary
    ):
        monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
        screens = [_FakeScreen(QRect(0, 0, 50, 40))]
        monkeypatch.setattr(capture, "QGuiApplication", _FakeQGuiApplication(screens))

        recorded_argv = []

        def fake_run(argv, **kwargs):
            recorded_argv.append(argv)
            return _placeholder_png_writer()(argv, **kwargs)

        monkeypatch.setattr(capture.subprocess, "run", fake_run)

        frame = backend_cls().capture()

        assert recorded_argv[0][0] == binary
        assert recorded_argv[0][-1] != binary  # a real path argument, not just the binary
        assert frame.logical_origin == QPointF(0, 0)
        assert frame.logical_size == QSizeF(50, 40)
        assert not frame.image.isNull()

    @pytest.mark.parametrize("backend_cls,binary", BACKENDS)
    def test_capture_raises_when_the_tool_exits_nonzero(self, monkeypatch, backend_cls, binary):
        monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
        screens = [_FakeScreen(QRect(0, 0, 50, 40))]
        monkeypatch.setattr(capture, "QGuiApplication", _FakeQGuiApplication(screens))

        def raising_run(argv, **kwargs):
            raise subprocess.CalledProcessError(1, argv)

        monkeypatch.setattr(capture.subprocess, "run", raising_run)

        with pytest.raises(subprocess.CalledProcessError):
            backend_cls().capture()

    def test_capture_cleans_up_its_temp_file_even_on_failure(self, monkeypatch):
        # Regression guard for the finally-block cleanup: a failing run
        # must not leave the temp path behind.
        monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
        screens = [_FakeScreen(QRect(0, 0, 50, 40))]
        monkeypatch.setattr(capture, "QGuiApplication", _FakeQGuiApplication(screens))

        seen_paths = []

        def raising_run(argv, **kwargs):
            seen_paths.append(argv[-1])
            raise subprocess.CalledProcessError(1, argv)

        monkeypatch.setattr(capture.subprocess, "run", raising_run)

        with pytest.raises(subprocess.CalledProcessError):
            capture.MaimBackend().capture()

        assert not os.path.exists(seen_paths[0])


class TestX11RegistryOrdering:
    def test_registers_backends_in_the_required_order(self):
        registry = capture.build_x11_registry()

        assert [backend.name() for backend in registry] == [
            "qt-native",
            "maim",
            "import",
            "scrot",
        ]


class TestX11RegistryFailover:
    def test_capture_falls_through_past_failing_backends(self, monkeypatch):
        monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
        monkeypatch.setattr(capture.shutil, "which", lambda b: f"/usr/bin/{b}")
        screens = [_FakeScreen(QRect(0, 0, 50, 40))]
        monkeypatch.setattr(capture, "QGuiApplication", _FakeQGuiApplication(screens))
        monkeypatch.setattr(
            capture.QtNativeX11Backend,
            "capture",
            Mock(side_effect=RuntimeError("qt-native boom")),
        )
        monkeypatch.setattr(
            capture.MaimBackend, "capture", Mock(side_effect=RuntimeError("maim boom"))
        )
        monkeypatch.setattr(capture.subprocess, "run", _placeholder_png_writer())

        registry = capture.build_x11_registry()
        frame = registry.capture()

        assert isinstance(frame, Frame)


class TestX11WindowGeometryProvider:
    WMCTRL_STDOUT = (
        "0x02c00003  0 1920 0   1024 768 host1 Firefox\n"
        "0x02c00007  0 100  200 640  480 host1 Terminal — bash\n"
    )

    def test_list_windows_parses_titles_and_geometry(self, monkeypatch):
        monkeypatch.setattr(capture.shutil, "which", lambda b: "/usr/bin/wmctrl")
        monkeypatch.setattr(
            capture.subprocess,
            "run",
            lambda *a, **k: Mock(stdout=self.WMCTRL_STDOUT, returncode=0),
        )

        windows = capture.X11WindowGeometryProvider().list_windows()

        assert windows == [
            ("Firefox", QRectF(1920, 0, 1024, 768)),
            ("Terminal — bash", QRectF(100, 200, 640, 480)),
        ]

    def test_list_windows_is_empty_when_wmctrl_missing(self, monkeypatch):
        monkeypatch.setattr(capture.shutil, "which", lambda b: None)

        assert capture.X11WindowGeometryProvider().list_windows() == []

    def test_list_windows_is_empty_when_wmctrl_fails(self, monkeypatch):
        monkeypatch.setattr(capture.shutil, "which", lambda b: "/usr/bin/wmctrl")

        def raising_run(*a, **k):
            raise subprocess.CalledProcessError(1, ["wmctrl"])

        monkeypatch.setattr(capture.subprocess, "run", raising_run)

        assert capture.X11WindowGeometryProvider().list_windows() == []

    def test_window_at_returns_the_containing_rect_or_none(self, monkeypatch):
        monkeypatch.setattr(capture.shutil, "which", lambda b: "/usr/bin/wmctrl")
        monkeypatch.setattr(
            capture.subprocess,
            "run",
            lambda *a, **k: Mock(stdout=self.WMCTRL_STDOUT, returncode=0),
        )
        provider = capture.X11WindowGeometryProvider()

        assert provider.window_at(QPointF(200, 300)) == QRectF(100, 200, 640, 480)
        assert provider.window_at(QPointF(5, 5)) is None

    def test_window_at_reuses_cached_list_within_the_cache_window(self, monkeypatch):
        # overlay.py's window mode calls window_at() once per mouseMoveEvent
        # — far more often than a process can be spawned and reaped — so a
        # burst of calls close together in time must not spawn `wmctrl`
        # more than once, or hover-highlight would stutter behind the
        # cursor (see REVIEW.md).
        monkeypatch.setattr(capture.shutil, "which", lambda b: "/usr/bin/wmctrl")
        run = Mock(return_value=Mock(stdout=self.WMCTRL_STDOUT, returncode=0))
        monkeypatch.setattr(capture.subprocess, "run", run)
        clock = [100.0]
        monkeypatch.setattr(capture.time, "monotonic", lambda: clock[0])
        provider = capture.X11WindowGeometryProvider()

        provider.window_at(QPointF(200, 300))
        clock[0] += 0.05
        provider.window_at(QPointF(5, 5))
        clock[0] += 0.05
        provider.window_at(QPointF(1920, 0))

        assert run.call_count == 1

    def test_list_windows_refetches_once_the_cache_expires(self, monkeypatch):
        monkeypatch.setattr(capture.shutil, "which", lambda b: "/usr/bin/wmctrl")
        run = Mock(return_value=Mock(stdout=self.WMCTRL_STDOUT, returncode=0))
        monkeypatch.setattr(capture.subprocess, "run", run)
        clock = [100.0]
        monkeypatch.setattr(capture.time, "monotonic", lambda: clock[0])
        provider = capture.X11WindowGeometryProvider()

        provider.list_windows()
        clock[0] += capture.X11WindowGeometryProvider._CACHE_SECONDS + 0.01
        provider.list_windows()

        assert run.call_count == 2

    def test_is_available_requires_x11_and_the_binary(self, monkeypatch):
        provider = capture.X11WindowGeometryProvider()

        monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
        monkeypatch.setattr(capture.shutil, "which", lambda b: "/usr/bin/wmctrl")
        assert provider.is_available() is True

        monkeypatch.setattr(capture.shutil, "which", lambda b: None)
        assert provider.is_available() is False

        monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
        monkeypatch.setattr(capture.shutil, "which", lambda b: "/usr/bin/wmctrl")
        assert provider.is_available() is False


WAYLAND_ONLY_BACKENDS = [
    capture.GrimBackend,
    capture.PortalScreenshotBackend,
    capture.GnomeShellHelperBackend,
]

NOT_WAYLAND_SESSION_TYPES = ["x11", None]  # None means XDG_SESSION_TYPE unset


class TestWaylandBackendsRequireWaylandSession:
    """Covers "Wayland backends report unavailable when detect_session_type()
    is not 'wayland'" directly for each backend, rather than relying on one
    shared assumption — with grim/D-Bus mocked as present so the session
    type is provably the only thing gating availability here.
    """

    @pytest.mark.parametrize("backend_cls", WAYLAND_ONLY_BACKENDS)
    @pytest.mark.parametrize("session_type", NOT_WAYLAND_SESSION_TYPES)
    def test_unavailable_when_not_wayland(self, monkeypatch, backend_cls, session_type):
        _set_session_type(monkeypatch, session_type)
        monkeypatch.setattr(capture.shutil, "which", lambda b: f"/usr/bin/{b}")

        assert backend_cls().is_available() is False


class TestGrimBackend:
    def test_unavailable_when_binary_missing(self, monkeypatch):
        monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
        monkeypatch.setattr(capture.shutil, "which", lambda b: None)

        backend = capture.GrimBackend()

        assert backend.is_available() is False
        assert "grim" in backend.unavailable_reason()

    def test_unavailable_off_wayland_even_with_binary_present(self, monkeypatch):
        monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
        monkeypatch.setattr(capture.shutil, "which", lambda b: f"/usr/bin/{b}")

        backend = capture.GrimBackend()

        assert backend.is_available() is False
        assert backend.unavailable_reason() == "not a Wayland session"

    def test_available_when_wayland_and_binary_present(self, monkeypatch):
        monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
        monkeypatch.setattr(capture.shutil, "which", lambda b: f"/usr/bin/{b}")

        backend = capture.GrimBackend()

        assert backend.is_available() is True
        assert backend.unavailable_reason() is None

    def test_capture_invokes_grim_and_returns_a_frame(self, monkeypatch):
        monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
        screens = [_FakeScreen(QRect(0, 0, 50, 40))]
        monkeypatch.setattr(capture, "QGuiApplication", _FakeQGuiApplication(screens))

        recorded_argv = []

        def fake_run(argv, **kwargs):
            recorded_argv.append(argv)
            return _placeholder_png_writer()(argv, **kwargs)

        monkeypatch.setattr(capture.subprocess, "run", fake_run)

        frame = capture.GrimBackend().capture()

        assert recorded_argv[0][0] == "grim"
        assert recorded_argv[0][-1] != "grim"  # a real path argument, not just the binary
        assert frame.logical_origin == QPointF(0, 0)
        assert frame.logical_size == QSizeF(50, 40)
        assert not frame.image.isNull()

    def test_capture_cleans_up_its_temp_file_even_on_failure(self, monkeypatch):
        monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
        screens = [_FakeScreen(QRect(0, 0, 50, 40))]
        monkeypatch.setattr(capture, "QGuiApplication", _FakeQGuiApplication(screens))

        seen_paths = []

        def raising_run(argv, **kwargs):
            seen_paths.append(argv[-1])
            raise subprocess.CalledProcessError(1, argv)

        monkeypatch.setattr(capture.subprocess, "run", raising_run)

        with pytest.raises(subprocess.CalledProcessError):
            capture.GrimBackend().capture()

        assert not os.path.exists(seen_paths[0])


class TestPortalScreenshotBackend:
    def test_is_available_only_under_wayland(self, monkeypatch):
        backend = capture.PortalScreenshotBackend()

        monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
        assert backend.is_available() is True

        monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
        assert backend.is_available() is False

    def test_unavailable_reason_matches_availability(self, monkeypatch):
        backend = capture.PortalScreenshotBackend()

        monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
        assert backend.unavailable_reason() is None

        monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
        assert backend.unavailable_reason() == "not a Wayland session"

    def test_subscribe_happens_before_send_request(self, monkeypatch, tmp_path):
        # The acceptance criterion this backend exists for: a fast-replying
        # portal's Response signal must never be able to arrive before this
        # backend has started listening for it.
        monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
        order = []

        fake_filter_handle = Mock()
        fake_filter_handle.queue = Mock()

        def fake_subscribe(self, connection, request_path):
            order.append("subscribe")
            return fake_filter_handle

        def fake_send_request(self, connection, handle_token):
            order.append("send_request")

        monkeypatch.setattr(capture.PortalScreenshotBackend, "_subscribe", fake_subscribe)
        monkeypatch.setattr(
            capture.PortalScreenshotBackend, "_send_request", fake_send_request
        )

        image_path = tmp_path / "shot.png"
        _write_placeholder_png(str(image_path))
        response = Mock(body=(0, {"uri": ("s", image_path.as_uri())}))
        fake_connection = Mock(unique_name=":1.23")
        fake_connection.recv_until_filtered = Mock(return_value=response)
        monkeypatch.setattr(capture, "open_dbus_connection", lambda bus: fake_connection)

        frame = capture.PortalScreenshotBackend().capture()

        assert order == ["subscribe", "send_request"]
        assert isinstance(frame, Frame)

    def test_capture_builds_a_frame_from_the_response_signal(self, monkeypatch, tmp_path):
        monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
        screens = [_FakeScreen(QRect(0, 0, 50, 40))]
        monkeypatch.setattr(capture, "QGuiApplication", _FakeQGuiApplication(screens))

        image_path = tmp_path / "shot.png"
        _write_placeholder_png(str(image_path))
        response = Mock(body=(0, {"uri": ("s", image_path.as_uri())}))

        fake_connection = Mock(unique_name=":1.42")
        fake_filter_handle = Mock()
        fake_filter_handle.queue = Mock()
        fake_connection.filter = Mock(return_value=fake_filter_handle)
        fake_connection.recv_until_filtered = Mock(return_value=response)
        monkeypatch.setattr(capture, "open_dbus_connection", lambda bus: fake_connection)

        frame = capture.PortalScreenshotBackend().capture()

        assert fake_connection.filter.called
        assert fake_connection.send.called
        assert fake_connection.close.called
        assert frame.logical_origin == QPointF(0, 0)
        assert frame.logical_size == QSizeF(50, 40)
        assert not frame.image.isNull()

    def test_capture_sends_modal_false_so_a_windowless_caller_still_gets_a_dialog(
        self, monkeypatch, tmp_path
    ):
        # SNX-67: a process the keybinding just spawned has no window yet,
        # so it has no parent to hand the portal for a *modal* dialog. If
        # the request leaves `modal` at the spec default (true), GNOME's
        # portal backend refuses without ever showing the dialog. Asserting
        # the actual option sent is what pins this down, rather than just
        # trusting a comment.
        monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
        screens = [_FakeScreen(QRect(0, 0, 50, 40))]
        monkeypatch.setattr(capture, "QGuiApplication", _FakeQGuiApplication(screens))

        image_path = tmp_path / "shot.png"
        _write_placeholder_png(str(image_path))
        response = Mock(body=(0, {"uri": ("s", image_path.as_uri())}))

        fake_connection = Mock(unique_name=":1.42")
        fake_filter_handle = Mock()
        fake_filter_handle.queue = Mock()
        fake_connection.filter = Mock(return_value=fake_filter_handle)
        fake_connection.recv_until_filtered = Mock(return_value=response)
        monkeypatch.setattr(capture, "open_dbus_connection", lambda bus: fake_connection)

        capture.PortalScreenshotBackend().capture()

        sent_message = fake_connection.send.call_args[0][0]
        _parent_window, options = sent_message.body
        assert options["modal"] == ("b", False)
        assert options["interactive"] == ("b", False)

    @pytest.mark.parametrize(
        "response_code,expected_text",
        [
            (1, "cancelled"),
            (2, "response code 2"),
            (3, "response code 3"),
        ],
    )
    def test_capture_raises_when_response_code_is_nonzero(
        self, monkeypatch, response_code, expected_text
    ):
        # response code 1 = user cancelled, 2 = other error (and anything
        # else the spec doesn't define falls in with 2). Either way
        # `results` carries no "uri", so this must fail with a clear
        # RuntimeError rather than a bare KeyError on results["uri"] — and
        # cancelled vs. error must read as distinct messages, each telling
        # the user what to do about it, not a real portal being involved.
        monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")

        response = Mock(body=(response_code, {}))
        fake_connection = Mock(unique_name=":1.42")
        fake_filter_handle = Mock()
        fake_filter_handle.queue = Mock()
        fake_connection.filter = Mock(return_value=fake_filter_handle)
        fake_connection.recv_until_filtered = Mock(return_value=response)
        monkeypatch.setattr(capture, "open_dbus_connection", lambda bus: fake_connection)

        with pytest.raises(RuntimeError, match=expected_text):
            capture.PortalScreenshotBackend().capture()

        assert fake_connection.close.called

    def test_cancelled_and_error_messages_are_distinct_and_actionable(self, monkeypatch):
        # Belt-and-braces on top of the parametrized test above: cancelled
        # tells the user to retry and approve the prompt, while an error
        # points at the portal installation instead — mixing those up would
        # send a cancelled user chasing a package install that isn't broken.
        monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")

        def make_connection(response_code):
            response = Mock(body=(response_code, {}))
            fake_connection = Mock(unique_name=":1.42")
            fake_filter_handle = Mock()
            fake_filter_handle.queue = Mock()
            fake_connection.filter = Mock(return_value=fake_filter_handle)
            fake_connection.recv_until_filtered = Mock(return_value=response)
            return fake_connection

        monkeypatch.setattr(
            capture, "open_dbus_connection", lambda bus: make_connection(1)
        )
        with pytest.raises(RuntimeError) as cancelled_excinfo:
            capture.PortalScreenshotBackend().capture()

        monkeypatch.setattr(
            capture, "open_dbus_connection", lambda bus: make_connection(2)
        )
        with pytest.raises(RuntimeError) as error_excinfo:
            capture.PortalScreenshotBackend().capture()

        cancelled_message = str(cancelled_excinfo.value)
        error_message = str(error_excinfo.value)
        assert cancelled_message != error_message
        assert "press the shortcut again" in cancelled_message
        assert "xdg-desktop-portal" in error_message


class TestGnomeShellHelperBackend:
    def test_is_available_only_under_wayland(self, monkeypatch):
        backend = capture.GnomeShellHelperBackend()

        monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
        assert backend.is_available() is True

        monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
        assert backend.is_available() is False

    def test_unavailable_reason_matches_availability(self, monkeypatch):
        backend = capture.GnomeShellHelperBackend()

        monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
        assert backend.unavailable_reason() is None

        monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
        assert backend.unavailable_reason() == "not a Wayland session"

    def test_capture_returns_a_frame_when_screenshot_succeeds(self, monkeypatch):
        monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
        screens = [_FakeScreen(QRect(0, 0, 50, 40))]
        monkeypatch.setattr(capture, "QGuiApplication", _FakeQGuiApplication(screens))

        def fake_send_and_get_reply(message):
            # The filename this backend supplied is the call's last arg;
            # write the placeholder there so the backend's own QImage load
            # succeeds, exactly like the shell-out backends' fake_run does.
            path = message.body[-1]
            _write_placeholder_png(path)
            return Mock(body=(True, path))

        fake_connection = Mock()
        fake_connection.send_and_get_reply = Mock(side_effect=fake_send_and_get_reply)
        monkeypatch.setattr(capture, "open_dbus_connection", lambda bus: fake_connection)

        frame = capture.GnomeShellHelperBackend().capture()

        assert frame.logical_origin == QPointF(0, 0)
        assert frame.logical_size == QSizeF(50, 40)
        assert not frame.image.isNull()
        assert fake_connection.close.called

    def test_capture_raises_when_screenshot_reports_failure(self, monkeypatch):
        monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
        screens = [_FakeScreen(QRect(0, 0, 50, 40))]
        monkeypatch.setattr(capture, "QGuiApplication", _FakeQGuiApplication(screens))

        fake_connection = Mock()
        fake_connection.send_and_get_reply = Mock(return_value=Mock(body=(False, "")))
        monkeypatch.setattr(capture, "open_dbus_connection", lambda bus: fake_connection)

        with pytest.raises(RuntimeError):
            capture.GnomeShellHelperBackend().capture()

    def test_capture_cleans_up_its_temp_file_on_success_and_on_failure(self, monkeypatch):
        monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
        screens = [_FakeScreen(QRect(0, 0, 50, 40))]
        monkeypatch.setattr(capture, "QGuiApplication", _FakeQGuiApplication(screens))

        seen_paths = []

        def fake_send_and_get_reply_success(message):
            path = message.body[-1]
            seen_paths.append(path)
            _write_placeholder_png(path)
            return Mock(body=(True, path))

        fake_connection = Mock()
        fake_connection.send_and_get_reply = Mock(side_effect=fake_send_and_get_reply_success)
        monkeypatch.setattr(capture, "open_dbus_connection", lambda bus: fake_connection)

        capture.GnomeShellHelperBackend().capture()

        assert not os.path.exists(seen_paths[0])

        def fake_send_and_get_reply_failure(message):
            path = message.body[-1]
            seen_paths.append(path)
            return Mock(body=(False, path))

        fake_connection.send_and_get_reply = Mock(side_effect=fake_send_and_get_reply_failure)

        with pytest.raises(RuntimeError):
            capture.GnomeShellHelperBackend().capture()

        assert not os.path.exists(seen_paths[1])


class TestWaylandRegistryOrdering:
    def test_registers_backends_in_the_required_order(self):
        registry = capture.build_wayland_registry()

        assert [backend.name() for backend in registry] == [
            "grim",
            "portal",
            "gnome-shell-helper",
        ]


class TestWaylandRegistryFailover:
    def test_capture_falls_through_past_failing_backends(self, monkeypatch):
        monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
        monkeypatch.setattr(capture.shutil, "which", lambda b: f"/usr/bin/{b}")
        good_frame = make_frame()
        monkeypatch.setattr(
            capture.GrimBackend, "capture", Mock(side_effect=RuntimeError("grim boom"))
        )
        monkeypatch.setattr(
            capture.PortalScreenshotBackend,
            "capture",
            Mock(side_effect=RuntimeError("portal boom")),
        )
        monkeypatch.setattr(
            capture.GnomeShellHelperBackend, "capture", Mock(return_value=good_frame)
        )

        registry = capture.build_wayland_registry()
        frame = registry.capture()

        assert frame is good_frame
