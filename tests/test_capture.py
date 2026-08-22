from PyQt6.QtCore import QPointF, QRectF, QSizeF
from PyQt6.QtGui import QImage, qRgb

import pytest

from snipux.capture import (
    BackendRegistry,
    CaptureBackend,
    CaptureError,
    Frame,
    detect_session_type,
)


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
