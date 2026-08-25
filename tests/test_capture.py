import ctypes
import os
import subprocess
from types import SimpleNamespace
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


class _FakeUser32:
    """Stand-in for `ctypes.windll.user32`, covering only what
    `Win32GdiBackend` calls: `GetSystemMetrics`, `GetDC`, `ReleaseDC`.
    `metrics` maps a `GetSystemMetrics` index straight to the value the
    test wants it to report.
    """

    def __init__(self, metrics, dc=123):
        self._metrics = metrics
        self._dc = dc
        self.released = []

    def GetSystemMetrics(self, index):
        return self._metrics[index]

    def GetDC(self, hwnd):
        return self._dc

    def ReleaseDC(self, hwnd, dc):
        self.released.append(dc)
        return 1


class _FakeGdi32:
    """Stand-in for `ctypes.windll.gdi32`, covering only what
    `Win32GdiBackend._blit_to_image` calls. `pixel_bytes`, if given, is
    what `GetDIBits` fills the caller's buffer with; otherwise a small
    non-uniform BGRA pattern so a test can tell real pixels came back
    rather than a zeroed buffer.
    """

    def __init__(self, pixel_bytes=None, bitblt_ok=True, getdibits_ok=True):
        self._pixel_bytes = pixel_bytes
        self._bitblt_ok = bitblt_ok
        self._getdibits_ok = getdibits_ok
        self.blit_args = None

    def CreateCompatibleDC(self, dc):
        return 111

    def CreateCompatibleBitmap(self, dc, width, height):
        return 222

    def SelectObject(self, dc, obj):
        return 333

    def BitBlt(self, dest_dc, x, y, width, height, src_dc, src_x, src_y, rop):
        self.blit_args = (dest_dc, x, y, width, height, src_dc, src_x, src_y, rop)
        return 1 if self._bitblt_ok else 0

    def GetDIBits(self, dc, bitmap, start, lines, buffer, header_ref, usage):
        if not self._getdibits_ok:
            return 0
        # capture.py passes the header via ctypes.byref(), so it arrives
        # here as a CArgObject rather than the struct itself -- cast it
        # back to read biWidth/biHeight, the same way the real Win32 API
        # would after ctypes' own argument marshaling.
        header = ctypes.cast(
            header_ref, ctypes.POINTER(capture._BitmapInfoHeader)
        ).contents
        pixel_count = header.biWidth * abs(header.biHeight)
        pattern = self._pixel_bytes or bytes([10, 20, 30, 255])
        ctypes.memmove(buffer, pattern * pixel_count, pixel_count * 4)
        return 1

    def DeleteObject(self, obj):
        return 1

    def DeleteDC(self, dc):
        return 1


def _patch_win32_dll(monkeypatch, user32, gdi32):
    """Points `capture.ctypes.windll.user32`/`.gdi32` at the given fakes --
    `raising=False` because `ctypes.windll` doesn't exist at all off
    Windows, and this suite runs on both.
    """
    monkeypatch.setattr(
        capture.ctypes, "windll", SimpleNamespace(user32=user32, gdi32=gdi32), raising=False
    )


class _FakeUser32Windows:
    """Stand-in for `ctypes.windll.user32`, covering what
    `WindowsWindowGeometryProvider` calls: `EnumWindows`, `IsWindowVisible`,
    `IsIconic`, `GetWindowRect`, `GetWindowTextLengthW`, `GetWindowTextW`,
    `GetClassNameW`, `MonitorFromWindow`, `GetMonitorInfoW`.

    `windows` is the list of `hwnd`/`visible`/`iconic`/`rect`/`title` dicts
    `EnumWindows` should hand to the caller's callback, one at a time, in
    the order given -- tests put them in the Z-order (topmost first) the
    real API would visit them in. `class_name` is optional and defaults to
    `""` (an ordinary application window, never one of
    `WindowsWindowGeometryProvider._SHELL_CLASS_NAMES`).

    `monitors` maps a `hwnd` to the `(left, top, right, bottom)` rect of
    the monitor it sits on (SNX-94's "larger than its own monitor" check)
    -- a `hwnd` missing from it gets a monitor large enough to contain any
    rect a test doesn't care about, so every test written before that
    check existed keeps working unchanged.
    """

    _DEFAULT_MONITOR_RECT = (-1_000_000, -1_000_000, 1_000_000, 1_000_000)

    def __init__(self, windows, monitors=None):
        self._windows = {w["hwnd"]: w for w in windows}
        self._order = [w["hwnd"] for w in windows]
        self._monitors = monitors or {}

    def EnumWindows(self, callback, lparam):
        for hwnd in self._order:
            if not callback(hwnd, lparam):
                break
        return 1

    def IsWindowVisible(self, hwnd):
        return 1 if self._windows[hwnd]["visible"] else 0

    def IsIconic(self, hwnd):
        return 1 if self._windows[hwnd]["iconic"] else 0

    def GetWindowRect(self, hwnd, rect_ref):
        left, top, right, bottom = self._windows[hwnd]["rect"]
        target = ctypes.cast(rect_ref, ctypes.POINTER(capture._RECT)).contents
        target.left, target.top, target.right, target.bottom = left, top, right, bottom
        return 1

    def GetWindowTextLengthW(self, hwnd):
        return len(self._windows[hwnd]["title"])

    def GetWindowTextW(self, hwnd, buffer, _size):
        buffer.value = self._windows[hwnd]["title"]
        return len(buffer.value)

    def GetClassNameW(self, hwnd, buffer, _size):
        class_name = self._windows[hwnd].get("class_name", "")
        buffer.value = class_name
        return len(class_name)

    def MonitorFromWindow(self, hwnd, _flags):
        # The real API returns an opaque HMONITOR; the hwnd itself is a
        # perfectly good, distinguishable stand-in since this fake never
        # has two windows share a monitor rect by aliasing handles.
        return hwnd

    def GetMonitorInfoW(self, hmonitor, info_ref):
        left, top, right, bottom = self._monitors.get(hmonitor, self._DEFAULT_MONITOR_RECT)
        target = ctypes.cast(info_ref, ctypes.POINTER(capture._MonitorInfo)).contents
        target.rcMonitor.left, target.rcMonitor.top = left, top
        target.rcMonitor.right, target.rcMonitor.bottom = right, bottom
        return 1


class _FakeDwmapi:
    """Stand-in for `ctypes.windll.dwmapi`'s one entry point
    `WindowsWindowGeometryProvider` calls, `DwmGetWindowAttribute`, for both
    attributes it asks about: `DWMWA_CLOAKED` and
    `DWMWA_EXTENDED_FRAME_BOUNDS`.

    `cloaked` maps hwnd -> truthy/falsy. `frame_bounds` maps hwnd -> an
    (left, top, right, bottom) tuple; a hwnd missing from it makes the
    extended-frame-bounds call fail (a non-zero HRESULT), the same as DWM
    itself refusing to answer -- exactly the case
    `WindowsWindowGeometryProvider._frame_bounds` falls back to
    `GetWindowRect` for.
    """

    def __init__(self, cloaked=None, frame_bounds=None):
        self._cloaked = cloaked or {}
        self._frame_bounds = frame_bounds or {}

    def DwmGetWindowAttribute(self, hwnd, attribute, out_ref, _size):
        if attribute == capture.WindowsWindowGeometryProvider._DWMWA_CLOAKED:
            target = ctypes.cast(out_ref, ctypes.POINTER(ctypes.c_int)).contents
            target.value = 1 if self._cloaked.get(hwnd) else 0
            return 0
        if attribute == capture.WindowsWindowGeometryProvider._DWMWA_EXTENDED_FRAME_BOUNDS:
            bounds = self._frame_bounds.get(hwnd)
            if bounds is None:
                return 1  # a failing HRESULT: DWM couldn't answer
            left, top, right, bottom = bounds
            target = ctypes.cast(out_ref, ctypes.POINTER(capture._RECT)).contents
            target.left, target.top, target.right, target.bottom = left, top, right, bottom
            return 0
        raise AssertionError(f"unexpected DwmGetWindowAttribute attribute {attribute!r}")


def _patch_windows_geometry_dll(monkeypatch, user32, dwmapi):
    """Points `capture.ctypes.windll.user32`/`.dwmapi` at the given fakes,
    the `WindowsWindowGeometryProvider` counterpart of `_patch_win32_dll`
    above. Also stands `ctypes.WINFUNCTYPE` in for
    `capture.ctypes.WINFUNCTYPE` when the suite runs off Windows, where the
    real one doesn't exist at all (it's defined only under `sys.platform ==
    "win32"` in the stdlib itself) -- `ctypes.CFUNCTYPE` builds the same
    kind of Python-callable function pointer and is available everywhere,
    which is all `_list_windows_uncached`'s enum callback needs from it in
    a test that never actually crosses into real Win32 code.
    """
    monkeypatch.setattr(
        capture.ctypes,
        "windll",
        SimpleNamespace(user32=user32, dwmapi=dwmapi),
        raising=False,
    )
    monkeypatch.setattr(capture.ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE, raising=False)


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
        # Pinned off Windows: this suite runs there too, and the advice
        # branches on sys.platform before it ever looks at session type.
        monkeypatch.setattr(capture.sys, "platform", "linux")
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
        monkeypatch.setattr(capture.sys, "platform", "linux")
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
        monkeypatch.setattr(capture.sys, "platform", "linux")
        monkeypatch.delenv("XDG_SESSION_TYPE", raising=False)
        registry = BackendRegistry([])

        with pytest.raises(CaptureError) as excinfo:
            registry.capture()

        message = str(excinfo.value)
        assert message.startswith("no capture backend is available:")
        assert not message.rstrip().endswith(":")
        assert "grim" in message and "maim" in message

    def test_capture_names_windows_not_grim_maim_or_apt_when_no_backend_is_available(
        self, monkeypatch
    ):
        # SNX-88: before this, a Windows failure fell through to
        # session-type-based advice (which always reads "unknown" there,
        # since XDG_SESSION_TYPE is a Linux/X11/Wayland concept) and told a
        # Windows user to `sudo apt install grim maim` -- advice for a
        # different OS entirely.
        monkeypatch.setattr(capture.sys, "platform", "win32")
        registry = BackendRegistry([FakeBackend("b", False, reason="not running on Windows")])

        with pytest.raises(CaptureError) as excinfo:
            registry.capture()

        message = str(excinfo.value)
        assert message.startswith("no capture backend is available:")
        assert "Windows" in message
        assert "grim" not in message
        assert "maim" not in message
        assert "apt install" not in message


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


class TestXwininfoWindowGeometryProvider:
    """Window mode's fallback for X11 sessions without `wmctrl`.

    `wmctrl` is not installed by default on Ubuntu, so without this the mode
    was in the menu, picking it did nothing visible, and the only clue was a
    toast -- indistinguishable from the feature being broken.
    """

    SAMPLE = (
        '  0x3400011 "Terminal": ("terminal" "Terminal")  866x629+10+20  +504+215\n'
        '  0x400007 "mutter guard window": ()  6400x1440+0+0  +0+0\n'
        '  0x2200004 "tiny": ()  1x1+0+0  +0+0\n'
        '  0x2200005 "Brave": ("brave" "Brave")  1920x1080+0+0  +0+201\n'
    )

    def _provider(self, monkeypatch, stdout=SAMPLE):
        provider = capture.XwininfoWindowGeometryProvider()
        monkeypatch.setattr(capture.shutil, "which", lambda name: "/usr/bin/xwininfo")
        monkeypatch.setattr(
            capture.subprocess, "run",
            lambda *a, **k: SimpleNamespace(stdout=stdout, returncode=0),
        )
        return provider

    def test_it_reads_absolute_geometry(self, monkeypatch):
        provider = self._provider(monkeypatch)

        windows = dict(provider.list_windows())

        assert windows["Terminal"] == QRectF(504, 215, 866, 629)

    def test_the_compositors_backdrop_is_not_a_window(self, monkeypatch):
        # It spans the whole desktop and sits above everything, so left in
        # the list it would be the answer to every hover.
        provider = self._provider(monkeypatch)

        assert "mutter guard window" not in dict(provider.list_windows())

    def test_helper_windows_are_dropped(self, monkeypatch):
        provider = self._provider(monkeypatch)

        assert "tiny" not in dict(provider.list_windows())

    def test_the_topmost_window_wins_a_hover(self, monkeypatch):
        # xwininfo lists children bottom-of-stack first, so the list has to
        # be reversed or a hover resolves to whatever is underneath.
        provider = self._provider(monkeypatch)

        assert provider.list_windows()[0][0] == "Brave"

    def test_a_miss_returns_none(self, monkeypatch):
        provider = self._provider(monkeypatch)

        assert provider.window_at(QPointF(99999, 99999)) is None

    def test_a_failing_xwininfo_yields_no_windows_rather_than_raising(self, monkeypatch):
        provider = capture.XwininfoWindowGeometryProvider()
        monkeypatch.setattr(capture.shutil, "which", lambda name: "/usr/bin/xwininfo")

        def boom(*args, **kwargs):
            raise OSError("nope")

        monkeypatch.setattr(capture.subprocess, "run", boom)

        assert provider.list_windows() == []

    def test_it_is_unavailable_without_xwininfo(self, monkeypatch):
        monkeypatch.setattr(capture.shutil, "which", lambda name: None)

        assert not capture.XwininfoWindowGeometryProvider().is_available()


class TestBuildLinuxRegistry:
    """SNX-86: `platform.linux.LinuxPlatform.build_capture_registry()`
    forwards here -- this is where the session-type branching that used to
    live in `app.build_default_registry()` actually lives now.
    """

    def test_returns_wayland_registry_on_a_wayland_session(self, monkeypatch):
        _set_session_type(monkeypatch, "wayland")

        registry = capture.build_linux_registry()

        assert [b.name() for b in registry] == [
            b.name() for b in capture.build_wayland_registry()
        ]

    def test_returns_x11_registry_on_an_x11_session(self, monkeypatch):
        _set_session_type(monkeypatch, "x11")

        registry = capture.build_linux_registry()

        assert [b.name() for b in registry] == [
            b.name() for b in capture.build_x11_registry()
        ]

    def test_returns_both_registries_concatenated_on_an_unknown_session(self, monkeypatch):
        # Neither registry is preferred: every backend gates itself with its
        # own is_available(), so offering both is how an unrecognised
        # session type still finds whatever is actually installed instead
        # of failing outright.
        _set_session_type(monkeypatch, None)

        registry = capture.build_linux_registry()

        expected = [b.name() for b in capture.build_wayland_registry()] + [
            b.name() for b in capture.build_x11_registry()
        ]
        assert [b.name() for b in registry] == expected


class TestQtNativeWindowsBackend:
    """SNX-88: verified against a real three-monitor Windows desktop (one
    screen to the right of the primary, one above-and-left of it) that
    `QScreen.grabWindow(0)`, grabbed per-screen and composited the same way
    `QtNativeX11Backend` already does, returns each monitor's real pixels
    rather than the black image Wayland hands back or a copy of just the
    primary display.
    """

    def test_is_available_only_on_windows(self, monkeypatch):
        backend = capture.QtNativeWindowsBackend()

        monkeypatch.setattr(capture.sys, "platform", "win32")
        assert backend.is_available() is True

        monkeypatch.setattr(capture.sys, "platform", "linux")
        assert backend.is_available() is False

    def test_unavailable_reason_matches_availability(self, monkeypatch):
        backend = capture.QtNativeWindowsBackend()

        monkeypatch.setattr(capture.sys, "platform", "win32")
        assert backend.unavailable_reason() is None

        monkeypatch.setattr(capture.sys, "platform", "darwin")
        assert backend.unavailable_reason() == "not running on Windows"

    def test_capture_covers_every_monitor_including_one_above_and_left(self, monkeypatch):
        monkeypatch.setattr(capture.sys, "platform", "win32")
        # Mirrors the real desktop this backend was verified against:
        # negative x *and* negative y in the same virtual desktop.
        screens = [
            _FakeScreen(QRect(0, 0, 2560, 1440)),
            _FakeScreen(QRect(2560, 0, 2560, 1440)),
            _FakeScreen(QRect(1164, -1440, 2560, 1440)),
        ]
        monkeypatch.setattr(capture, "QGuiApplication", _FakeQGuiApplication(screens))

        frame = capture.QtNativeWindowsBackend().capture()

        assert frame.logical_origin == QPointF(0, -1440)
        assert frame.logical_size == QSizeF(5120, 2880)
        assert frame.image.width() == 5120
        assert frame.image.height() == 2880
        assert not frame.image.isNull()

    def test_capture_raises_when_a_screens_grab_comes_back_empty(self, monkeypatch):
        # The "only returns the primary display" failure mode the ticket
        # warns about must not be silently painted as black -- it has to
        # raise, so BackendRegistry.capture() falls through to the Win32
        # GDI backend instead of handing back a frame missing a monitor.
        monkeypatch.setattr(capture.sys, "platform", "win32")

        class _NullGrabScreen(_FakeScreen):
            def grabWindow(self, window_id):
                return QPixmap()

        screens = [
            _FakeScreen(QRect(0, 0, 100, 50)),
            _NullGrabScreen(QRect(100, 0, 100, 50)),
        ]
        monkeypatch.setattr(capture, "QGuiApplication", _FakeQGuiApplication(screens))

        with pytest.raises(RuntimeError):
            capture.QtNativeWindowsBackend().capture()


class TestWin32GdiBackend:
    """SNX-88: the fallback for when qt-native doesn't cover the whole
    virtual desktop -- a single BitBlt of the region GetSystemMetrics
    reports as the virtual screen, via ctypes (no new dependency).
    """

    def test_is_available_only_on_windows(self, monkeypatch):
        backend = capture.Win32GdiBackend()

        monkeypatch.setattr(capture.sys, "platform", "win32")
        assert backend.is_available() is True

        monkeypatch.setattr(capture.sys, "platform", "linux")
        assert backend.is_available() is False

    def test_unavailable_reason_matches_availability(self, monkeypatch):
        backend = capture.Win32GdiBackend()

        monkeypatch.setattr(capture.sys, "platform", "win32")
        assert backend.unavailable_reason() is None

        monkeypatch.setattr(capture.sys, "platform", "darwin")
        assert backend.unavailable_reason() == "not running on Windows"

    def test_capture_blits_the_gsm_reported_region_in_one_call(self, monkeypatch):
        monkeypatch.setattr(capture.sys, "platform", "win32")
        # A monitor left of the primary, like _virtual_desktop_geometry()
        # sees on X11's equivalent test -- the logical rect this backend
        # reports must still come from Qt, not from GDI's own numbers.
        screens = [_FakeScreen(QRect(-100, 0, 300, 200))]
        monkeypatch.setattr(capture, "QGuiApplication", _FakeQGuiApplication(screens))
        user32 = _FakeUser32({76: -100, 77: 0, 78: 300, 79: 200})
        gdi32 = _FakeGdi32()
        _patch_win32_dll(monkeypatch, user32, gdi32)

        frame = capture.Win32GdiBackend().capture()

        assert gdi32.blit_args is not None
        _dest_dc, dest_x, dest_y, width, height, _src_dc, src_x, src_y, _rop = gdi32.blit_args
        assert (dest_x, dest_y) == (0, 0)
        assert (width, height) == (300, 200)
        assert (src_x, src_y) == (-100, 0)
        assert frame.image.width() == 300
        assert frame.image.height() == 200
        assert not frame.image.isNull()
        assert frame.logical_origin == QPointF(-100, 0)
        assert frame.logical_size == QSizeF(300, 200)

    def test_capture_releases_the_screen_dc_even_when_bitblt_fails(self, monkeypatch):
        monkeypatch.setattr(capture.sys, "platform", "win32")
        screens = [_FakeScreen(QRect(0, 0, 100, 50))]
        monkeypatch.setattr(capture, "QGuiApplication", _FakeQGuiApplication(screens))
        user32 = _FakeUser32({76: 0, 77: 0, 78: 100, 79: 50})
        gdi32 = _FakeGdi32(bitblt_ok=False)
        _patch_win32_dll(monkeypatch, user32, gdi32)

        with pytest.raises(RuntimeError, match="BitBlt"):
            capture.Win32GdiBackend().capture()

        assert user32.released == [user32._dc]

    def test_capture_raises_when_get_dibits_fails(self, monkeypatch):
        monkeypatch.setattr(capture.sys, "platform", "win32")
        screens = [_FakeScreen(QRect(0, 0, 100, 50))]
        monkeypatch.setattr(capture, "QGuiApplication", _FakeQGuiApplication(screens))
        user32 = _FakeUser32({76: 0, 77: 0, 78: 100, 79: 50})
        gdi32 = _FakeGdi32(getdibits_ok=False)
        _patch_win32_dll(monkeypatch, user32, gdi32)

        with pytest.raises(RuntimeError, match="GetDIBits"):
            capture.Win32GdiBackend().capture()

    def test_capture_raises_when_the_reported_virtual_screen_is_empty(self, monkeypatch):
        monkeypatch.setattr(capture.sys, "platform", "win32")
        user32 = _FakeUser32({76: 0, 77: 0, 78: 0, 79: 0})
        gdi32 = _FakeGdi32()
        _patch_win32_dll(monkeypatch, user32, gdi32)

        with pytest.raises(RuntimeError, match="GetSystemMetrics"):
            capture.Win32GdiBackend().capture()


class TestBuildWindowsRegistry:
    """SNX-88: `platform.windows.WindowsPlatform.build_capture_registry()`
    forwards here, the same way Linux's does to `build_linux_registry()`.
    """

    def test_registers_backends_in_the_required_order(self):
        registry = capture.build_windows_registry()

        assert [backend.name() for backend in registry] == ["qt-native", "win32-gdi"]

    def test_capture_falls_through_from_qt_native_to_the_gdi_backend(self, monkeypatch):
        monkeypatch.setattr(capture.sys, "platform", "win32")
        screens = [_FakeScreen(QRect(0, 0, 100, 50))]
        monkeypatch.setattr(capture, "QGuiApplication", _FakeQGuiApplication(screens))
        monkeypatch.setattr(
            capture.QtNativeWindowsBackend,
            "capture",
            Mock(side_effect=RuntimeError("qt-native boom")),
        )
        user32 = _FakeUser32({76: 0, 77: 0, 78: 100, 79: 50})
        gdi32 = _FakeGdi32()
        _patch_win32_dll(monkeypatch, user32, gdi32)

        registry = capture.build_windows_registry()
        frame = registry.capture()

        assert isinstance(frame, Frame)
        assert not frame.image.isNull()


class TestWindowsWindowGeometryProvider:
    """SNX-90: `EnumWindows`/`GetWindowRect`/`DwmGetWindowAttribute` via
    ctypes -- Windows' counterpart to `TestX11WindowGeometryProvider`
    above. Unlike X11, Windows has no "no client may enumerate other
    windows" restriction to work around, so this is available whenever
    `sys.platform == "win32"`, no external tool required.
    """

    def test_is_available_only_on_windows(self, monkeypatch):
        provider = capture.WindowsWindowGeometryProvider()

        monkeypatch.setattr(capture.sys, "platform", "win32")
        assert provider.is_available() is True

        monkeypatch.setattr(capture.sys, "platform", "linux")
        assert provider.is_available() is False

    def test_list_windows_uses_extended_frame_bounds_not_get_window_rect(
        self, monkeypatch
    ):
        # The invisible resize border GetWindowRect would include: its
        # rect (0,0,120,120) is padded 10px past what DWM's own extended
        # frame bounds (10,10,100,100) report as actually visible.
        monkeypatch.setattr(capture.sys, "platform", "win32")
        user32 = _FakeUser32Windows(
            [{"hwnd": 1, "visible": True, "iconic": False,
              "rect": (0, 0, 120, 120), "title": "Notepad"}]
        )
        dwmapi = _FakeDwmapi(frame_bounds={1: (10, 10, 100, 100)})
        _patch_windows_geometry_dll(monkeypatch, user32, dwmapi)

        windows = capture.WindowsWindowGeometryProvider().list_windows()

        assert windows == [("Notepad", QRectF(10, 10, 90, 90))]

    def test_list_windows_falls_back_to_get_window_rect_when_dwm_fails(
        self, monkeypatch
    ):
        monkeypatch.setattr(capture.sys, "platform", "win32")
        user32 = _FakeUser32Windows(
            [{"hwnd": 1, "visible": True, "iconic": False,
              "rect": (0, 0, 50, 50), "title": "Old-style window"}]
        )
        # No entry for hwnd 1 in frame_bounds -- DwmGetWindowAttribute
        # reports failure, same as a pre-DWM window.
        dwmapi = _FakeDwmapi()
        _patch_windows_geometry_dll(monkeypatch, user32, dwmapi)

        windows = capture.WindowsWindowGeometryProvider().list_windows()

        assert windows == [("Old-style window", QRectF(0, 0, 50, 50))]

    def test_list_windows_skips_hidden_minimised_and_cloaked_windows(
        self, monkeypatch
    ):
        monkeypatch.setattr(capture.sys, "platform", "win32")
        user32 = _FakeUser32Windows([
            {"hwnd": 1, "visible": False, "iconic": False,
             "rect": (0, 0, 50, 50), "title": "Hidden"},
            {"hwnd": 2, "visible": True, "iconic": True,
             "rect": (0, 0, 50, 50), "title": "Minimised"},
            {"hwnd": 3, "visible": True, "iconic": False,
             "rect": (0, 0, 50, 50), "title": "Cloaked"},
            {"hwnd": 4, "visible": True, "iconic": False,
             "rect": (0, 0, 50, 50), "title": "Visible"},
        ])
        dwmapi = _FakeDwmapi(
            cloaked={3: True},
            frame_bounds={
                1: (0, 0, 50, 50), 2: (0, 0, 50, 50),
                3: (0, 0, 50, 50), 4: (0, 0, 50, 50),
            },
        )
        _patch_windows_geometry_dll(monkeypatch, user32, dwmapi)

        windows = capture.WindowsWindowGeometryProvider().list_windows()

        assert [title for title, _rect in windows] == ["Visible"]

    def test_list_windows_skips_the_shells_desktop_and_workspace_windows(
        self, monkeypatch
    ):
        # SNX-94: Progman/WorkerW are real, visible, non-minimised
        # top-level windows -- small ones here, deliberately well within
        # their monitor, to prove class name alone (not size) is what
        # excludes them.
        monkeypatch.setattr(capture.sys, "platform", "win32")
        user32 = _FakeUser32Windows([
            {"hwnd": 1, "visible": True, "iconic": False, "class_name": "Progman",
             "rect": (0, 0, 50, 50), "title": ""},
            {"hwnd": 2, "visible": True, "iconic": False, "class_name": "WorkerW",
             "rect": (0, 0, 50, 50), "title": ""},
            {"hwnd": 3, "visible": True, "iconic": False, "class_name": "Notepad",
             "rect": (0, 0, 50, 50), "title": "Notepad"},
        ])
        dwmapi = _FakeDwmapi(
            frame_bounds={1: (0, 0, 50, 50), 2: (0, 0, 50, 50), 3: (0, 0, 50, 50)}
        )
        _patch_windows_geometry_dll(monkeypatch, user32, dwmapi)

        windows = capture.WindowsWindowGeometryProvider().list_windows()

        assert [title for title, _rect in windows] == ["Notepad"]

    def test_list_windows_skips_the_taskbar(self, monkeypatch):
        # SNX-94's other named case: hovering the taskbar must not offer
        # it as a window either. Shell_SecondaryTrayWnd covers a
        # secondary monitor's taskbar, the same way Shell_TrayWnd covers
        # the primary one.
        monkeypatch.setattr(capture.sys, "platform", "win32")
        user32 = _FakeUser32Windows([
            {"hwnd": 1, "visible": True, "iconic": False, "class_name": "Shell_TrayWnd",
             "rect": (0, 1040, 1920, 1080), "title": ""},
            {"hwnd": 2, "visible": True, "iconic": False,
             "class_name": "Shell_SecondaryTrayWnd",
             "rect": (1920, 1040, 3840, 1080), "title": ""},
            {"hwnd": 3, "visible": True, "iconic": False, "class_name": "Notepad",
             "rect": (0, 0, 50, 50), "title": "Notepad"},
        ])
        dwmapi = _FakeDwmapi(
            frame_bounds={
                1: (0, 1040, 1920, 1080), 2: (1920, 1040, 3840, 1080), 3: (0, 0, 50, 50),
            }
        )
        _patch_windows_geometry_dll(monkeypatch, user32, dwmapi)

        windows = capture.WindowsWindowGeometryProvider().list_windows()

        assert [title for title, _rect in windows] == ["Notepad"]

    def test_list_windows_drops_a_window_larger_than_its_monitor(self, monkeypatch):
        # SNX-94: probing empty desktop returned a rect the size of the
        # whole virtual desktop (every monitor combined), as though it
        # were a window -- this is the generic, class-name-independent
        # guard against exactly that, in case something other than
        # Progman/WorkerW is ever sized past its own monitor.
        monkeypatch.setattr(capture.sys, "platform", "win32")
        user32 = _FakeUser32Windows(
            [{"hwnd": 1, "visible": True, "iconic": False, "class_name": "",
              "rect": (0, 0, 5120, 2880), "title": "Spans every monitor"}],
            monitors={1: (0, 0, 1920, 1080)},
        )
        dwmapi = _FakeDwmapi(frame_bounds={1: (0, 0, 5120, 2880)})
        _patch_windows_geometry_dll(monkeypatch, user32, dwmapi)

        windows = capture.WindowsWindowGeometryProvider().list_windows()

        assert windows == []

    def test_list_windows_keeps_a_genuine_full_screen_window(self, monkeypatch):
        # The counterpart to the previous test: a window exactly the size
        # of the monitor it's on (a borderless-fullscreen game, say) is
        # not "larger than" that monitor and must still be offered.
        monkeypatch.setattr(capture.sys, "platform", "win32")
        user32 = _FakeUser32Windows(
            [{"hwnd": 1, "visible": True, "iconic": False, "class_name": "",
              "rect": (0, 0, 1920, 1080), "title": "Fullscreen game"}],
            monitors={1: (0, 0, 1920, 1080)},
        )
        dwmapi = _FakeDwmapi(frame_bounds={1: (0, 0, 1920, 1080)})
        _patch_windows_geometry_dll(monkeypatch, user32, dwmapi)

        windows = capture.WindowsWindowGeometryProvider().list_windows()

        assert windows == [("Fullscreen game", QRectF(0, 0, 1920, 1080))]

    def test_window_at_returns_none_when_only_the_desktop_is_under_the_point(
        self, monkeypatch
    ):
        # The actual bug report: hovering an empty area of the desktop
        # must offer no window, not the whole virtual desktop.
        monkeypatch.setattr(capture.sys, "platform", "win32")
        user32 = _FakeUser32Windows(
            [{"hwnd": 1, "visible": True, "iconic": False, "class_name": "Progman",
              "rect": (0, 0, 5120, 2880), "title": ""}]
        )
        dwmapi = _FakeDwmapi(frame_bounds={1: (0, 0, 5120, 2880)})
        _patch_windows_geometry_dll(monkeypatch, user32, dwmapi)

        assert capture.WindowsWindowGeometryProvider().window_at(QPointF(2500, 1400)) is None

    def test_window_at_picks_an_ordinary_window_on_a_negative_coordinate_monitor(
        self, monkeypatch
    ):
        # A monitor placed above/left of the primary has a negative
        # origin; an ordinary window on it must still be picked, and must
        # not be mistaken for spanning past its own (also
        # negative-origin) monitor.
        monkeypatch.setattr(capture.sys, "platform", "win32")
        user32 = _FakeUser32Windows(
            [{"hwnd": 1, "visible": True, "iconic": False, "class_name": "Notepad",
              "rect": (-1920, 0, -920, 1080), "title": "Notepad"}],
            monitors={1: (-1920, 0, 0, 1080)},
        )
        dwmapi = _FakeDwmapi(frame_bounds={1: (-1920, 0, -920, 1080)})
        _patch_windows_geometry_dll(monkeypatch, user32, dwmapi)

        rect = capture.WindowsWindowGeometryProvider().window_at(QPointF(-1500, 500))

        assert rect == QRectF(-1920, 0, 1000, 1080)

    def test_window_at_returns_the_topmost_of_several_overlapping_windows(
        self, monkeypatch
    ):
        # EnumWindows visits windows front-to-back, so the first (not
        # last) match in enumeration order must win -- unlike
        # XwininfoWindowGeometryProvider, this needs no reversal to get
        # there.
        monkeypatch.setattr(capture.sys, "platform", "win32")
        user32 = _FakeUser32Windows([
            {"hwnd": 1, "visible": True, "iconic": False,
             "rect": (0, 0, 100, 100), "title": "Front"},
            {"hwnd": 2, "visible": True, "iconic": False,
             "rect": (0, 0, 200, 200), "title": "Back"},
        ])
        dwmapi = _FakeDwmapi(
            frame_bounds={1: (0, 0, 100, 100), 2: (0, 0, 200, 200)}
        )
        _patch_windows_geometry_dll(monkeypatch, user32, dwmapi)
        provider = capture.WindowsWindowGeometryProvider()

        assert provider.window_at(QPointF(50, 50)) == QRectF(0, 0, 100, 100)
        assert provider.window_at(QPointF(150, 150)) == QRectF(0, 0, 200, 200)
        assert provider.window_at(QPointF(500, 500)) is None

    def test_list_windows_reuses_cached_list_within_the_cache_window(
        self, monkeypatch
    ):
        monkeypatch.setattr(capture.sys, "platform", "win32")
        user32 = _FakeUser32Windows(
            [{"hwnd": 1, "visible": True, "iconic": False,
              "rect": (0, 0, 50, 50), "title": "W"}]
        )
        dwmapi = _FakeDwmapi(frame_bounds={1: (0, 0, 50, 50)})
        _patch_windows_geometry_dll(monkeypatch, user32, dwmapi)
        enum_windows = Mock(wraps=user32.EnumWindows)
        monkeypatch.setattr(user32, "EnumWindows", enum_windows)
        clock = [100.0]
        monkeypatch.setattr(capture.time, "monotonic", lambda: clock[0])
        provider = capture.WindowsWindowGeometryProvider()

        provider.window_at(QPointF(10, 10))
        clock[0] += 0.05
        provider.window_at(QPointF(10, 10))

        assert enum_windows.call_count == 1

    def test_list_windows_refetches_once_the_cache_expires(self, monkeypatch):
        monkeypatch.setattr(capture.sys, "platform", "win32")
        user32 = _FakeUser32Windows(
            [{"hwnd": 1, "visible": True, "iconic": False,
              "rect": (0, 0, 50, 50), "title": "W"}]
        )
        dwmapi = _FakeDwmapi(frame_bounds={1: (0, 0, 50, 50)})
        _patch_windows_geometry_dll(monkeypatch, user32, dwmapi)
        enum_windows = Mock(wraps=user32.EnumWindows)
        monkeypatch.setattr(user32, "EnumWindows", enum_windows)
        clock = [100.0]
        monkeypatch.setattr(capture.time, "monotonic", lambda: clock[0])
        provider = capture.WindowsWindowGeometryProvider()

        provider.list_windows()
        clock[0] += capture.WindowsWindowGeometryProvider._CACHE_SECONDS + 0.01
        provider.list_windows()

        assert enum_windows.call_count == 2

    def test_list_windows_is_empty_off_windows(self, monkeypatch):
        monkeypatch.setattr(capture.sys, "platform", "linux")

        assert capture.WindowsWindowGeometryProvider().list_windows() == []


class TestUnsupportedPlatformBackend:
    """SNX-86: what `platform.darwin.DarwinPlatform.build_capture_registry()`
    registers in place of a real backend, until one exists (SNX-88 gave
    Windows a real one, so it no longer uses this placeholder).
    """

    def test_is_never_available(self):
        assert capture.UnsupportedPlatformBackend("Windows").is_available() is False

    def test_unavailable_reason_names_the_platform(self):
        backend = capture.UnsupportedPlatformBackend("Windows")

        assert "Windows" in backend.unavailable_reason()

    def test_capture_raises_rather_than_pretending_to_work(self):
        backend = capture.UnsupportedPlatformBackend("Windows")

        with pytest.raises(NotImplementedError):
            backend.capture()

    def test_registry_capture_reports_it_as_a_true_failure_not_silence(self):
        # is_available() is always False, so BackendRegistry.capture() skips
        # it entirely and CaptureError ends up with an empty failures list --
        # the same "no backend was even available to try" case a fresh
        # install with no tooling hits, which still has to produce a clear
        # message rather than a bare, empty-summary exception.
        registry = BackendRegistry([capture.UnsupportedPlatformBackend("Windows")])

        with pytest.raises(CaptureError) as excinfo:
            registry.capture()

        assert excinfo.value.failures == []
