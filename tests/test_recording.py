import threading
import time
from unittest.mock import Mock

from jeepney import HeaderFields

from PyQt6.QtCore import QRect, QRectF, QSize, Qt, QThread
from PyQt6.QtMultimedia import QMediaRecorder, QVideoFrame, QVideoFrameFormat
from PyQt6.QtWidgets import QApplication

import pytest

import snipux.recording as recording
from snipux.recording import (
    GnomeScreencastBackend,
    RecorderRegistry,
    RecordingBackend,
    RecordingError,
    WindowsRecorderBackend,
    _bytes_per_pixel,
    _clamp_rect_to_frame,
    _crop_frame,
    _rect_to_screen_pixels,
    _RegionCropWorker,
)


@pytest.fixture(scope="module", autouse=True)
def qapp():
    # WindowsRecorderBackend's region path reads QGuiApplication.primaryScreen()
    # -- None without a live QGuiApplication, even offscreen -- same reason
    # test_capture.py's Qt-native X11 backend tests need this fixture.
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


@pytest.fixture(autouse=True)
def _pinned_recording_settings(monkeypatch):
    """`GnomeScreencastBackend._screencast_options()` (SNX-124 ticket 9)
    reads `setup_desktop.load_recording_draw_cursor`/`load_recording_frame_rate`
    fresh on every call -- with no override, that hits whatever config
    directory this box's real user happens to have, which is exactly the
    kind of environment-dependent read a test suite must not depend on.
    Pinned here to the documented defaults; individual tests override
    either one to check the value actually reaches the D-Bus call.
    """
    monkeypatch.setattr(recording.setup_desktop, "load_recording_draw_cursor", lambda: True)
    monkeypatch.setattr(recording.setup_desktop, "load_recording_frame_rate", lambda: 30)


class FakeBackend(RecordingBackend):
    """Small `RecordingBackend` implementation for exercising the registry,
    mirroring `test_capture.py`'s `FakeBackend` style rather than mocking
    the ABC. Records every `start()`/`stop()` call it receives so a test
    can assert not just the return value but whether a later backend was
    ever reached at all.
    """

    def __init__(self, backend_name, available, reason=None, start_error=None):
        self._name = backend_name
        self._available = available
        self._reason = reason
        self._start_error = start_error
        self.start_calls = []
        self.stop_calls = 0

    def name(self):
        return self._name

    def is_available(self):
        return self._available

    def unavailable_reason(self):
        return self._reason

    def start(self, rect, path):
        self.start_calls.append((rect, path))
        if self._start_error is not None:
            raise self._start_error

    def stop(self):
        self.stop_calls += 1


class TestRecordingBackendIsAbstract:
    def test_cannot_be_instantiated_directly(self):
        with pytest.raises(TypeError):
            RecordingBackend()


class TestRecorderRegistryAvailable:
    def test_available_filters_to_available_backends_only(self):
        available_backend = FakeBackend("a", True)
        unavailable_backend = FakeBackend("b", False, reason="not implemented yet")
        registry = RecorderRegistry([available_backend, unavailable_backend])

        assert registry.available() == [available_backend]

    def test_available_preserves_registration_order(self):
        first = FakeBackend("first", True)
        second = FakeBackend("second", True)
        registry = RecorderRegistry([first, second])

        assert registry.available() == [first, second]


class TestRecorderRegistryUnavailable:
    def test_returns_exactly_the_backends_missing_from_available(self):
        available_backend = FakeBackend("a", True)
        unavailable_backend = FakeBackend("b", False, reason="not implemented yet")
        registry = RecorderRegistry([available_backend, unavailable_backend])

        assert registry.unavailable() == [("b", "not implemented yet")]

    def test_empty_when_every_backend_is_available(self):
        registry = RecorderRegistry([FakeBackend("a", True), FakeBackend("b", True)])

        assert registry.unavailable() == []

    def test_preserves_registration_order(self):
        registry = RecorderRegistry(
            [
                FakeBackend("first", False, reason="one"),
                FakeBackend("second", True),
                FakeBackend("third", False, reason="two"),
            ]
        )

        assert registry.unavailable() == [("first", "one"), ("third", "two")]


class TestRecorderRegistryStart:
    RECT = QRectF(0, 0, 100, 100)
    PATH = "/tmp/out.mp4"

    def test_returns_the_first_backend_that_succeeds(self):
        succeeding = FakeBackend("succeeding", True)
        registry = RecorderRegistry([succeeding])

        result = registry.start(self.RECT, self.PATH)

        assert result is succeeding
        assert succeeding.start_calls == [(self.RECT, self.PATH)]

    def test_does_not_call_start_on_backends_after_the_first_success(self):
        succeeding = FakeBackend("succeeding", True)
        never_tried = FakeBackend("never-tried", True)
        registry = RecorderRegistry([succeeding, never_tried])

        registry.start(self.RECT, self.PATH)

        assert never_tried.start_calls == []

    def test_a_failing_backend_does_not_stop_the_next_one_from_being_tried(self):
        failing = FakeBackend("failing", True, start_error=RuntimeError("boom"))
        succeeding = FakeBackend("succeeding", True)
        registry = RecorderRegistry([failing, succeeding])

        result = registry.start(self.RECT, self.PATH)

        assert result is succeeding
        assert failing.start_calls == [(self.RECT, self.PATH)]
        assert succeeding.start_calls == [(self.RECT, self.PATH)]

    def test_raises_with_every_failure_in_order_when_all_available_backends_fail(self):
        first_error = RuntimeError("first boom")
        second_error = RuntimeError("second boom")
        registry = RecorderRegistry(
            [
                FakeBackend("first", True, start_error=first_error),
                FakeBackend("second", True, start_error=second_error),
            ]
        )

        with pytest.raises(RecordingError) as excinfo:
            registry.start(self.RECT, self.PATH)

        assert excinfo.value.failures == [("first", first_error), ("second", second_error)]
        assert (
            str(excinfo.value)
            == "all recording backends failed: first: first boom; second: second boom"
        )

    def test_skips_unavailable_backends_entirely(self):
        unavailable = FakeBackend("unavailable", False, reason="not implemented yet")
        succeeding = FakeBackend("succeeding", True)
        registry = RecorderRegistry([unavailable, succeeding])

        registry.start(self.RECT, self.PATH)

        assert unavailable.start_calls == []


class TestRecordingErrorWhenNoBackendIsAvailable:
    def test_names_the_platform_and_enumerates_each_unavailable_backend(self, monkeypatch):
        monkeypatch.setattr(recording.sys, "platform", "win32")
        registry = RecorderRegistry(
            [FakeBackend("qt-native", False, reason="not implemented yet")]
        )

        with pytest.raises(RecordingError) as excinfo:
            registry.start(QRectF(0, 0, 10, 10), "/tmp/out.mp4")

        message = str(excinfo.value)
        assert excinfo.value.failures == []
        assert "Windows" in message
        assert "qt-native" in message
        assert "not implemented yet" in message

    def test_does_not_give_advice_for_a_different_platform(self, monkeypatch):
        # This is what makes "names the platform rather than giving advice
        # for a different one" machine-checkable: none of capture.py's
        # Linux-package advice terms should ever leak into a recording
        # message, on any platform, since there is no such fix for
        # recording yet.
        monkeypatch.setattr(recording.sys, "platform", "win32")
        registry = RecorderRegistry(
            [FakeBackend("qt-native", False, reason="not implemented yet")]
        )

        with pytest.raises(RecordingError) as excinfo:
            registry.start(QRectF(0, 0, 10, 10), "/tmp/out.mp4")

        message = str(excinfo.value)
        assert "grim" not in message
        assert "maim" not in message
        assert "apt install" not in message

    def test_names_linux_when_not_on_windows_or_macos(self, monkeypatch):
        monkeypatch.setattr(recording.sys, "platform", "linux")
        registry = RecorderRegistry(
            [FakeBackend("gnome-screencast", False, reason="not a GNOME session")]
        )

        with pytest.raises(RecordingError) as excinfo:
            registry.start(QRectF(0, 0, 10, 10), "/tmp/out.mp4")

        message = str(excinfo.value)
        assert "Linux" in message
        assert "gnome-screencast" in message
        assert "not a GNOME session" in message

    def test_names_macos(self, monkeypatch):
        monkeypatch.setattr(recording.sys, "platform", "darwin")
        registry = RecorderRegistry([FakeBackend("macos-native", False, reason="not implemented yet")])

        with pytest.raises(RecordingError) as excinfo:
            registry.start(QRectF(0, 0, 10, 10), "/tmp/out.mp4")

        assert "macOS" in str(excinfo.value)

    def test_falls_back_to_just_the_platform_sentence_when_nothing_is_registered(
        self, monkeypatch
    ):
        # This ticket's own end state: a bare registry with no backend
        # added at all. There's nothing to enumerate, so the message
        # shouldn't dangle a "(tried: )" with nothing after it.
        monkeypatch.setattr(recording.sys, "platform", "win32")
        registry = RecorderRegistry()

        with pytest.raises(RecordingError) as excinfo:
            registry.start(QRectF(0, 0, 10, 10), "/tmp/out.mp4")

        message = str(excinfo.value)
        assert message == "no recording backend is available on Windows"
        assert "tried" not in message

    def test_unavailable_is_reachable_from_the_exception_without_calling_back_into_the_registry(
        self, monkeypatch
    ):
        monkeypatch.setattr(recording.sys, "platform", "win32")
        registry = RecorderRegistry(
            [FakeBackend("qt-native", False, reason="not implemented yet")]
        )

        with pytest.raises(RecordingError) as excinfo:
            registry.start(QRectF(0, 0, 10, 10), "/tmp/out.mp4")

        assert excinfo.value.unavailable == [("qt-native", "not implemented yet")]


def _fake_connection(reply=None, reply_error=None):
    """A `Mock` standing in for jeepney's blocking connection, mirroring
    `test_capture.py`'s style of monkeypatching `open_dbus_connection`
    rather than talking to a real session bus -- there is none in headless
    CI.
    """
    connection = Mock()
    if reply_error is not None:
        connection.send_and_get_reply = Mock(side_effect=reply_error)
    else:
        connection.send_and_get_reply = Mock(return_value=reply)
    return connection


class TestGnomeScreencastBackendIsAvailable:
    def test_true_when_the_property_reports_supported(self, monkeypatch):
        connection = _fake_connection(reply=Mock(body=(("b", True),)))
        monkeypatch.setattr(recording, "open_dbus_connection", lambda bus: connection)

        backend = GnomeScreencastBackend()

        assert backend.is_available() is True
        assert backend.unavailable_reason() is None

    def test_false_with_a_reason_when_the_property_reports_unsupported(self, monkeypatch):
        connection = _fake_connection(reply=Mock(body=(("b", False),)))
        monkeypatch.setattr(recording, "open_dbus_connection", lambda bus: connection)

        backend = GnomeScreencastBackend()

        assert backend.is_available() is False
        reason = backend.unavailable_reason()
        assert reason is not None
        assert "ScreencastSupported" in reason

    def test_false_with_a_distinct_reason_when_the_lookup_raises(self, monkeypatch):
        # Connection refused, no such interface, no such property, or a
        # malformed reply are all real, distinct ways this can fail -- the
        # reason should name what actually went wrong, not reuse the
        # "unsupported" message above.
        monkeypatch.setattr(
            recording,
            "open_dbus_connection",
            lambda bus: (_ for _ in ()).throw(ConnectionRefusedError("no bus")),
        )

        backend = GnomeScreencastBackend()

        assert backend.is_available() is False
        reason = backend.unavailable_reason()
        assert reason is not None
        assert "ConnectionRefusedError" in reason
        assert "no bus" in reason

    def test_the_two_failure_reasons_are_distinct_from_each_other(self, monkeypatch):
        connection = _fake_connection(reply=Mock(body=(("b", False),)))
        monkeypatch.setattr(recording, "open_dbus_connection", lambda bus: connection)
        unsupported_reason = GnomeScreencastBackend().unavailable_reason()

        monkeypatch.setattr(
            recording,
            "open_dbus_connection",
            lambda bus: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        error_reason = GnomeScreencastBackend().unavailable_reason()

        assert unsupported_reason != error_reason


class TestGnomeScreencastBackendStart:
    RECT = QRectF(10.4, 20.6, 99.5, 49.2)
    PATH = "/tmp/snipux-recording.webm"

    def test_calls_screencast_area_with_rounded_geometry_and_configured_options(
        self, monkeypatch
    ):
        # SNX-124 ticket 9: draw-cursor/framerate now come from Settings
        # (setup_desktop), not the `{}` placeholder ticket 1 left behind.
        monkeypatch.setattr(recording.setup_desktop, "load_recording_draw_cursor", lambda: True)
        monkeypatch.setattr(recording.setup_desktop, "load_recording_frame_rate", lambda: 30)
        connection = _fake_connection(reply=Mock(body=(True, self.PATH)))
        monkeypatch.setattr(recording, "open_dbus_connection", lambda bus: connection)

        GnomeScreencastBackend().start(self.RECT, self.PATH)

        message = connection.send_and_get_reply.call_args[0][0]
        assert message.header.fields[HeaderFields.interface] == (
            "org.gnome.Shell.Screencast"
        )
        assert message.header.fields[HeaderFields.member] == "ScreencastArea"
        assert message.header.fields[HeaderFields.signature] == "iiiisa{sv}"
        # left/top/right/bottom rounded independently, then width/height
        # taken as the difference -- not independently-rounded width/height
        # -- same as Frame.crop() in capture.py.
        left = round(self.RECT.left())
        top = round(self.RECT.top())
        width = round(self.RECT.left() + self.RECT.width()) - left
        height = round(self.RECT.top() + self.RECT.height()) - top
        options = {"draw-cursor": ("b", True), "framerate": ("i", 30)}
        assert message.body == (left, top, width, height, self.PATH, options)

    def test_screencast_area_options_reflect_disabled_cursor_and_a_different_rate(
        self, monkeypatch
    ):
        monkeypatch.setattr(
            recording.setup_desktop, "load_recording_draw_cursor", lambda: False
        )
        monkeypatch.setattr(recording.setup_desktop, "load_recording_frame_rate", lambda: 15)
        connection = _fake_connection(reply=Mock(body=(True, self.PATH)))
        monkeypatch.setattr(recording, "open_dbus_connection", lambda bus: connection)

        GnomeScreencastBackend().start(self.RECT, self.PATH)

        message = connection.send_and_get_reply.call_args[0][0]
        options = message.body[-1]
        assert options == {"draw-cursor": ("b", False), "framerate": ("i", 15)}

    def test_closes_the_connection_after_a_successful_call(self, monkeypatch):
        connection = _fake_connection(reply=Mock(body=(True, self.PATH)))
        monkeypatch.setattr(recording, "open_dbus_connection", lambda bus: connection)

        GnomeScreencastBackend().start(self.RECT, self.PATH)

        assert connection.close.called

    def test_raises_when_screencast_area_reports_failure(self, monkeypatch):
        connection = _fake_connection(reply=Mock(body=(False, self.PATH)))
        monkeypatch.setattr(recording, "open_dbus_connection", lambda bus: connection)

        with pytest.raises(RuntimeError):
            GnomeScreencastBackend().start(self.RECT, self.PATH)

    def test_raises_naming_both_paths_when_gnome_resolves_to_a_different_filename(
        self, monkeypatch
    ):
        actual_path = "/home/user/Videos/Screencasts/from-2026.webm"
        connection = _fake_connection(reply=Mock(body=(True, actual_path)))
        monkeypatch.setattr(recording, "open_dbus_connection", lambda bus: connection)

        with pytest.raises(RuntimeError) as excinfo:
            GnomeScreencastBackend().start(self.RECT, self.PATH)

        message = str(excinfo.value)
        assert self.PATH in message
        assert actual_path in message


class TestGnomeScreencastBackendStartWholeScreen:
    """`rect=None` means the whole virtual desktop, per
    docs/design/recording.md's `Screencast(sa{sv})` route -- a distinct
    D-Bus call from `ScreencastArea`, not a monitor-sized region, since a
    region isn't reliably distinguishable from "whole screen" on a
    multi-monitor desktop.
    """

    PATH = "/tmp/snipux-recording-fullscreen.webm"

    def test_calls_screencast_with_no_geometry_and_configured_options(self, monkeypatch):
        # SNX-124 ticket 9: same options as ScreencastArea -- see that
        # class's own test for why this is no longer a bare `{}`.
        monkeypatch.setattr(recording.setup_desktop, "load_recording_draw_cursor", lambda: True)
        monkeypatch.setattr(recording.setup_desktop, "load_recording_frame_rate", lambda: 30)
        connection = _fake_connection(reply=Mock(body=(True, self.PATH)))
        monkeypatch.setattr(recording, "open_dbus_connection", lambda bus: connection)

        GnomeScreencastBackend().start(None, self.PATH)

        message = connection.send_and_get_reply.call_args[0][0]
        assert message.header.fields[HeaderFields.interface] == (
            "org.gnome.Shell.Screencast"
        )
        assert message.header.fields[HeaderFields.member] == "Screencast"
        assert message.header.fields[HeaderFields.signature] == "sa{sv}"
        options = {"draw-cursor": ("b", True), "framerate": ("i", 30)}
        assert message.body == (self.PATH, options)

    def test_closes_the_connection_after_a_successful_call(self, monkeypatch):
        connection = _fake_connection(reply=Mock(body=(True, self.PATH)))
        monkeypatch.setattr(recording, "open_dbus_connection", lambda bus: connection)

        GnomeScreencastBackend().start(None, self.PATH)

        assert connection.close.called

    def test_raises_when_screencast_reports_failure(self, monkeypatch):
        connection = _fake_connection(reply=Mock(body=(False, self.PATH)))
        monkeypatch.setattr(recording, "open_dbus_connection", lambda bus: connection)

        with pytest.raises(RuntimeError):
            GnomeScreencastBackend().start(None, self.PATH)

    def test_raises_naming_both_paths_when_gnome_resolves_to_a_different_filename(
        self, monkeypatch
    ):
        actual_path = "/home/user/Videos/Screencasts/from-2026.webm"
        connection = _fake_connection(reply=Mock(body=(True, actual_path)))
        monkeypatch.setattr(recording, "open_dbus_connection", lambda bus: connection)

        with pytest.raises(RuntimeError) as excinfo:
            GnomeScreencastBackend().start(None, self.PATH)

        message = str(excinfo.value)
        assert self.PATH in message
        assert actual_path in message


class TestGnomeScreencastBackendStop:
    def test_calls_stop_screencast_and_returns_cleanly_on_success(self, monkeypatch):
        connection = _fake_connection(reply=Mock(body=(True,)))
        monkeypatch.setattr(recording, "open_dbus_connection", lambda bus: connection)

        GnomeScreencastBackend().stop()  # must not raise

        message = connection.send_and_get_reply.call_args[0][0]
        assert message.header.fields[HeaderFields.member] == "StopScreencast"
        assert connection.close.called

    def test_raises_when_stop_screencast_reports_failure(self, monkeypatch):
        connection = _fake_connection(reply=Mock(body=(False,)))
        monkeypatch.setattr(recording, "open_dbus_connection", lambda bus: connection)

        with pytest.raises(RuntimeError):
            GnomeScreencastBackend().stop()

    def test_opens_a_fresh_connection_rather_than_reusing_starts(self, monkeypatch):
        # GNOME Shell tracks the in-progress recording server-side, keyed
        # to the caller's D-Bus unique name -- there's nothing for this
        # process to hold between start() and stop(), so each should open
        # and close its own connection independently.
        connections = []

        def make_connection(bus):
            connection = _fake_connection(reply=Mock(body=(True, "/tmp/out.webm")))
            connections.append(connection)
            return connection

        monkeypatch.setattr(recording, "open_dbus_connection", make_connection)

        backend = GnomeScreencastBackend()
        backend.start(QRectF(0, 0, 10, 10), "/tmp/out.webm")

        connections.clear()
        monkeypatch.setattr(
            recording,
            "open_dbus_connection",
            lambda bus: _fake_connection(reply=Mock(body=(True,))),
        )
        backend.stop()  # must not raise, and must not touch start()'s connection


class TestBuildLinuxRegistry:
    def test_registers_the_gnome_screencast_backend(self):
        registry = recording.build_linux_registry()

        assert [backend.name() for backend in registry] == ["gnome-screencast"]


# --- WindowsRecorderBackend -------------------------------------------------
#
# None of QScreenCapture/QVideoSink/QMediaRecorder/QVideoFrameInput can
# produce a real frame stream under QT_QPA_PLATFORM=offscreen -- there is no
# real screen for QScreenCapture to capture pixels *from*. So, mirroring
# GnomeScreencastBackend's tests monkeypatching open_dbus_connection instead
# of talking to a real session bus, these substitute small fakes (through
# WindowsRecorderBackend's constructor-injected factories) for the Qt
# Multimedia objects and drive them synchronously.
#
# What these tests can prove, per docs/design/recording.md's "what a green
# suite will and will not prove": which branch runs and what it constructs;
# the crop-rect and byte-copy arithmetic; that sendVideoFrame is only ever
# called in response to a ready signal, and that a frame arriving before the
# next one replaces rather than queues; error-signal-to-exception wiring;
# stop()'s teardown order; and that the crop actually executes on a
# different thread than the one that delivered the frame. What they cannot
# prove -- a real recording plays back, and 28fps at 1280x720 -- needs a
# real Windows session with a monitor and is out of pytest's reach entirely.


class _FakeSignal:
    """A minimal stand-in for a pyqtSignal on the plain (non-QObject) fakes
    below: enough for production code's `.connect(slot, type)` /
    `.disconnect(slot)` / (test-side) `.emit(*args)` to work, without
    needing a real Qt signal or a real cross-thread event loop for the
    fakes that don't need one -- see `_QueuedSignal` further down for the
    two spots (`QVideoSink`/`QVideoFrameInput`) that do.
    """

    def __init__(self):
        self.connections = []

    def connect(self, slot, type=None):
        self.connections.append((slot, type))

    def disconnect(self, slot=None):
        if slot is None:
            self.connections = []
            return
        before = len(self.connections)
        self.connections = [(s, t) for s, t in self.connections if s != slot]
        if len(self.connections) == before:
            raise TypeError("disconnect() failed: not connected")

    def emit(self, *args):
        for slot, _type in list(self.connections):
            slot(*args)


class FakeScreenCapture:
    def __init__(self, fail_message=None):
        self.errorOccurred = _FakeSignal()
        self._fail_message = fail_message
        self.active_calls = []

    def setActive(self, active):
        self.active_calls.append(active)
        if active and self._fail_message:
            self.errorOccurred.emit(None, self._fail_message)


class FakeRecorder:
    """Mirrors the real `QMediaRecorder` contract `WindowsRecorderBackend`
    actually uses: `errorChanged` (not `errorOccurred`) plus `error()`/
    `errorString()` -- see `WindowsRecorderBackend._wire_errors()`'s
    docstring for why `errorOccurred` itself is unusable in this
    PyQt6/QtMultimedia build.
    """

    def __init__(self, fail_message=None):
        self.errorChanged = _FakeSignal()
        self._fail_message = fail_message
        self._error = QMediaRecorder.Error.NoError
        self._error_string = ""
        self.media_format = None
        self.output_location = None
        self.record_calls = 0
        self.stop_calls = 0
        self.video_frame_rate = None
        self.set_video_frame_rate_calls = []
        self._state = QMediaRecorder.RecorderState.StoppedState

    def setMediaFormat(self, media_format):
        self.media_format = media_format

    def setOutputLocation(self, url):
        self.output_location = url

    def setVideoFrameRate(self, frame_rate):
        self.video_frame_rate = frame_rate
        self.set_video_frame_rate_calls.append(frame_rate)

    def record(self):
        self.record_calls += 1
        self._state = QMediaRecorder.RecorderState.RecordingState
        if self._fail_message:
            self._error = QMediaRecorder.Error.ResourceError
            self._error_string = self._fail_message
            self.errorChanged.emit()

    def stop(self):
        self.stop_calls += 1
        # Fakes settle synchronously -- real QMediaRecorder.stop() isn't
        # documented as synchronous, which is exactly why
        # WindowsRecorderBackend._wait_for_stopped() checks recorderState()
        # rather than assuming.
        self._state = QMediaRecorder.RecorderState.StoppedState

    def recorderState(self):
        return self._state

    def error(self):
        return self._error

    def errorString(self):
        return self._error_string


class FakeCaptureSession:
    def __init__(self):
        self.screen_capture = None
        self.recorder = None
        self.video_sink = None
        self.video_frame_input = None

    def setScreenCapture(self, screen_capture):
        self.screen_capture = screen_capture

    def setRecorder(self, recorder):
        self.recorder = recorder

    def setVideoSink(self, video_sink):
        self.video_sink = video_sink

    def setVideoFrameInput(self, video_frame_input):
        self.video_frame_input = video_frame_input


class FakeVideoSink:
    def __init__(self):
        self.videoFrameChanged = _FakeSignal()


class FakeVideoFrameInput:
    def __init__(self):
        self.readyToSendVideoFrame = _FakeSignal()
        self.sent_frames = []

    def sendVideoFrame(self, frame):
        self.sent_frames.append(frame)


class FakeThread(QThread):
    """A real `QThread` subclass, not a plain fake -- `_RegionCropWorker
    .moveToThread()` is a real `QObject` method and PyQt rejects anything
    that isn't an actual `QThread` there. Overriding `start()`/`quit()`/
    `wait()` records calls without ever spinning a real OS thread or event
    loop; `TestRegionCropRunsOffTheDeliveryThread` is the one test that
    needs a real running thread and uses a bare `QThread` instead of this.
    """

    def __init__(self):
        super().__init__()
        self.start_calls = 0
        self.quit_calls = 0
        self.wait_calls = 0

    def start(self):
        self.start_calls += 1

    def quit(self):
        self.quit_calls += 1

    def wait(self):
        self.wait_calls += 1


def _make_frame(width, height, pixel_value_at):
    """A real `QVideoFrame`, `Format_BGRA8888`, with every pixel's four
    bytes set to `pixel_value_at(row, col)` -- real because `_crop_frame`
    maps/reads/writes actual `QVideoFrame` buffers, which is exactly the
    row-wise-vs-toImage() distinction this ticket cares about, and PyQt6's
    QtMultimedia value types (unlike QScreenCapture itself) work fine
    under `QT_QPA_PLATFORM=offscreen`.
    """
    frame = QVideoFrame(
        QVideoFrameFormat(
            QSize(width, height), QVideoFrameFormat.PixelFormat.Format_BGRA8888
        )
    )
    assert frame.map(QVideoFrame.MapMode.WriteOnly)
    try:
        stride = frame.bytesPerLine(0)
        bits = frame.bits(0)
        bits.setsize(stride * height)
        for row in range(height):
            row_bytes = bytearray(stride)
            for col in range(width):
                value = pixel_value_at(row, col) & 0xFF
                row_bytes[col * 4 : col * 4 + 4] = bytes([value, value, value, value])
            bits[row * stride : row * stride + stride] = bytes(row_bytes)
    finally:
        frame.unmap()
    return frame


def _read_pixel(frame, row, col):
    assert frame.map(QVideoFrame.MapMode.ReadOnly)
    try:
        stride = frame.bytesPerLine(0)
        bits = frame.bits(0)
        bits.setsize(stride * frame.height())
        offset = row * stride + col * 4
        return bits[offset][0]
    finally:
        frame.unmap()


class TestRectToScreenPixels:
    def test_maps_a_rect_at_the_screen_origin_with_no_scaling(self):
        rect = QRectF(10, 20, 100, 50)
        screen_geometry = QRectF(0, 0, 1920, 1080)

        pixel_rect = _rect_to_screen_pixels(rect, screen_geometry, 1.0)

        assert pixel_rect == QRect(10, 20, 100, 50)

    def test_subtracts_the_screens_own_origin_first(self):
        # A monitor above/left of the primary sits at a negative logical
        # origin -- the rect must be made screen-local before scaling, the
        # same way Frame.crop() in capture.py does.
        rect = QRectF(-1900, 50, 200, 100)
        screen_geometry = QRectF(-1920, 0, 1920, 1080)

        pixel_rect = _rect_to_screen_pixels(rect, screen_geometry, 1.0)

        assert pixel_rect == QRect(20, 50, 200, 100)

    def test_scales_by_the_device_pixel_ratio(self):
        rect = QRectF(10, 20, 100, 50)
        screen_geometry = QRectF(0, 0, 1920, 1080)

        pixel_rect = _rect_to_screen_pixels(rect, screen_geometry, 2.0)

        assert pixel_rect == QRect(20, 40, 200, 100)

    def test_rounds_left_top_right_bottom_independently_not_width_height(self):
        # Same reasoning as Frame.crop() and
        # GnomeScreencastBackend._call_screencast_area(): rounding width
        # separately from x can drift a pixel off the position-derived
        # edges under fractional scaling.
        rect = QRectF(10.4, 20.6, 99.5, 49.2)
        screen_geometry = QRectF(0, 0, 1920, 1080)
        ratio = 1.0

        pixel_rect = _rect_to_screen_pixels(rect, screen_geometry, ratio)

        left = round(rect.left())
        top = round(rect.top())
        width = round(rect.left() + rect.width()) - left
        height = round(rect.top() + rect.height()) - top
        assert pixel_rect == QRect(left, top, width, height)


class TestClampRectToFrame:
    def test_passes_through_a_rect_already_inside_the_frame(self):
        assert _clamp_rect_to_frame(QRect(2, 1, 3, 2), 6, 4) == QRect(2, 1, 3, 2)

    def test_clamps_the_right_and_bottom_edges(self):
        # What _rect_to_screen_pixels() can hand back for a region dragged
        # flush against a screen edge, once device-pixel-ratio rounding is
        # in play: an edge one pixel past the frame's real buffer.
        assert _clamp_rect_to_frame(QRect(1278, 718, 4, 4), 1280, 720) == QRect(
            1278, 718, 2, 2
        )

    def test_clamps_a_left_top_origin_before_zero(self):
        assert _clamp_rect_to_frame(QRect(-2, -2, 4, 4), 1280, 720) == QRect(0, 0, 2, 2)

    def test_a_rect_already_flush_with_the_frame_edge_is_unchanged(self):
        assert _clamp_rect_to_frame(QRect(1180, 620, 100, 100), 1280, 720) == QRect(
            1180, 620, 100, 100
        )


class TestRectToScreenPixelsAtTheScreenEdgeUnderScaling:
    def test_a_region_hanging_past_the_screen_edge_is_clamped_not_corrupted(self):
        # The review's specific worry: a region dragged to (or past) the
        # exact edge of the screen, combined with a non-1.0 device pixel
        # ratio, produces a pixel rect that reaches past the frame
        # QScreenCapture actually delivers. A small stand-in screen keeps
        # _make_frame()'s pixel-by-pixel fill fast; the ratio and the
        # overshoot are what matter here, not the resolution.
        screen_geometry = QRectF(0, 0, 50, 40)
        ratio = 2.0
        rect = QRectF(45, 35, 10, 10)  # right/bottom edges land past the screen

        pixel_rect = _rect_to_screen_pixels(rect, screen_geometry, ratio)
        frame_width = round(screen_geometry.width() * ratio)
        frame_height = round(screen_geometry.height() * ratio)
        assert pixel_rect.left() + pixel_rect.width() > frame_width  # the overshoot exists
        assert pixel_rect.top() + pixel_rect.height() > frame_height

        clamped = _clamp_rect_to_frame(pixel_rect, frame_width, frame_height)
        assert clamped.left() + clamped.width() == frame_width
        assert clamped.top() + clamped.height() == frame_height

        # Cropping a real, frame-sized source with the *unclamped* rect
        # must not corrupt the last row/column or throw -- it comes back
        # clamped to the frame automatically.
        source = _make_frame(frame_width, frame_height, lambda row, col: (row + col) % 251)
        cropped = _crop_frame(source, pixel_rect)
        assert cropped.width() == clamped.width()
        assert cropped.height() == clamped.height()
        for row in range(cropped.height()):
            for col in range(cropped.width()):
                expected = (clamped.top() + row + clamped.left() + col) % 251
                assert _read_pixel(cropped, row, col) == expected


class TestBytesPerPixel:
    def test_bgra8888_is_four_bytes(self):
        assert _bytes_per_pixel(QVideoFrameFormat.PixelFormat.Format_BGRA8888) == 4


class TestCropFrame:
    def test_extracts_the_requested_sub_rectangle_row_wise(self):
        source = _make_frame(6, 4, lambda row, col: row * 10 + col)

        cropped = _crop_frame(source, QRect(2, 1, 3, 2))

        assert cropped.width() == 3
        assert cropped.height() == 2
        for row in range(2):
            for col in range(3):
                expected = (1 + row) * 10 + (2 + col)
                assert _read_pixel(cropped, row, col) == expected & 0xFF

    def test_does_not_touch_pixels_outside_the_crop(self):
        source = _make_frame(4, 4, lambda row, col: 200)

        cropped = _crop_frame(source, QRect(1, 1, 2, 2))

        assert cropped.width() == 2
        assert cropped.height() == 2

    def test_source_frame_is_unmapped_after_cropping(self):
        source = _make_frame(4, 4, lambda row, col: 1)

        _crop_frame(source, QRect(0, 0, 2, 2))

        assert not source.isMapped()

    def test_clamps_a_rect_hanging_one_pixel_past_the_right_and_bottom_edges(self):
        # What a region dragged flush against a screen edge can produce
        # after _rect_to_screen_pixels()'s device-pixel-ratio rounding:
        # right/bottom one pixel past the source frame's actual buffer.
        # Slicing at the unclamped offsets would read (and size the output
        # frame) past the end of the last row instead of raising or
        # corrupting silently.
        source = _make_frame(4, 4, lambda row, col: row * 10 + col)

        cropped = _crop_frame(source, QRect(2, 2, 3, 3))

        assert cropped.width() == 2
        assert cropped.height() == 2
        for row in range(2):
            for col in range(2):
                expected = (2 + row) * 10 + (2 + col)
                assert _read_pixel(cropped, row, col) == expected & 0xFF

    def test_clamps_a_rect_starting_before_the_top_left_origin(self):
        source = _make_frame(4, 4, lambda row, col: row * 10 + col)

        cropped = _crop_frame(source, QRect(-1, -1, 3, 3))

        assert cropped.width() == 2
        assert cropped.height() == 2
        for row in range(2):
            for col in range(2):
                assert _read_pixel(cropped, row, col) == row * 10 + col

    def test_preserves_the_source_frames_start_and_end_time(self):
        # SNX-125: a freshly constructed QVideoFrame defaults startTime()/
        # endTime() to -1 (untimed). Nothing else in this module ever
        # copies the source frame's real capture timestamps onto the
        # cropped copy, so the encoder fell back to laying whatever it
        # received down at its own nominal spacing -- this is the direct
        # regression test for that.
        source = _make_frame(4, 4, lambda row, col: 1)
        source.setStartTime(123_456)
        source.setEndTime(156_789)

        cropped = _crop_frame(source, QRect(0, 0, 2, 2))

        assert cropped.startTime() == 123_456
        assert cropped.endTime() == 156_789


class TestRegionCropWorker:
    def test_does_nothing_when_no_frame_is_pending(self):
        frame_input = FakeVideoFrameInput()
        worker = _RegionCropWorker(frame_input, QRect(0, 0, 2, 2))

        worker.on_ready_to_send()

        assert frame_input.sent_frames == []

    def test_sends_the_cropped_pending_frame_when_ready(self):
        frame_input = FakeVideoFrameInput()
        worker = _RegionCropWorker(frame_input, QRect(0, 0, 2, 2))
        frame = _make_frame(4, 4, lambda row, col: 1)

        worker.on_frame(frame)
        worker.on_ready_to_send()

        assert len(frame_input.sent_frames) == 1
        assert frame_input.sent_frames[0].width() == 2
        assert frame_input.sent_frames[0].height() == 2

    def test_a_second_frame_replaces_the_pending_one_rather_than_queuing(self):
        # The 47-of-48-dropped failure mode was calling sendVideoFrame()
        # while the encoder was still refusing. Coalescing to "at most one
        # pending frame" avoids that by construction: a frame that arrives
        # before the next readyToSendVideoFrame supersedes the previous
        # one, and sendVideoFrame() is only ever called from
        # on_ready_to_send.
        frame_input = FakeVideoFrameInput()
        worker = _RegionCropWorker(frame_input, QRect(0, 0, 2, 2))
        first = _make_frame(4, 4, lambda row, col: 10)
        second = _make_frame(4, 4, lambda row, col: 20)

        worker.on_frame(first)
        worker.on_frame(second)
        worker.on_ready_to_send()

        assert len(frame_input.sent_frames) == 1
        assert _read_pixel(frame_input.sent_frames[0], 0, 0) == 20

    def test_ready_signal_with_nothing_pending_after_a_send_sends_nothing_more(self):
        frame_input = FakeVideoFrameInput()
        worker = _RegionCropWorker(frame_input, QRect(0, 0, 2, 2))
        frame = _make_frame(4, 4, lambda row, col: 5)

        worker.on_frame(frame)
        worker.on_ready_to_send()
        worker.on_ready_to_send()

        assert len(frame_input.sent_frames) == 1

    def test_a_ready_signal_arriving_before_the_first_frame_still_sends_it(self):
        # Confirmed by hand against the real QVideoFrameInput:
        # readyToSendVideoFrame fires once immediately, before any frame
        # has been delivered. A worker that only ever sent from inside
        # on_ready_to_send (bailing out when nothing was pending yet)
        # missed that edge and then stalled forever -- nothing calls
        # sendVideoFrame(), so no later readyToSendVideoFrame ever fires,
        # and zero frames reach the recorder. This is the race that
        # produced a real, empty 0-byte output file.
        frame_input = FakeVideoFrameInput()
        worker = _RegionCropWorker(frame_input, QRect(0, 0, 2, 2))
        frame = _make_frame(4, 4, lambda row, col: 7)

        worker.on_ready_to_send()
        worker.on_frame(frame)

        assert len(frame_input.sent_frames) == 1

    def test_does_not_send_a_second_time_until_ready_fires_again(self):
        frame_input = FakeVideoFrameInput()
        worker = _RegionCropWorker(frame_input, QRect(0, 0, 2, 2))
        first = _make_frame(4, 4, lambda row, col: 1)
        second = _make_frame(4, 4, lambda row, col: 2)

        worker.on_ready_to_send()
        worker.on_frame(first)  # sent immediately -- ready was already true
        worker.on_frame(second)  # not ready again yet -- must not send

        assert len(frame_input.sent_frames) == 1

    def test_the_surviving_frame_carries_its_own_timing_not_the_dropped_ones(self):
        # SNX-125: coalescing to "at most one pending frame" is correct and
        # load-bearing (see the class docstring) -- the bug was never the
        # dropping itself, only that the frame which survived carried no
        # timing. Two source frames with distinct real timestamps, both
        # delivered before the encoder asks for one: the frame that reaches
        # sendVideoFrame() must carry the *second* (surviving) frame's own
        # start/end time, not the first's and not an average of the two.
        frame_input = FakeVideoFrameInput()
        worker = _RegionCropWorker(frame_input, QRect(0, 0, 2, 2))
        first = _make_frame(4, 4, lambda row, col: 10)
        first.setStartTime(0)
        first.setEndTime(33_333)
        second = _make_frame(4, 4, lambda row, col: 20)
        second.setStartTime(33_333)
        second.setEndTime(66_666)

        worker.on_frame(first)
        worker.on_frame(second)
        worker.on_ready_to_send()

        assert len(frame_input.sent_frames) == 1
        sent = frame_input.sent_frames[0]
        assert sent.startTime() == 33_333
        assert sent.endTime() == 66_666


class TestRegionCropWorkerDuration:
    """The ticket's own acceptance criterion: "a test records for a fixed
    wall-clock interval and fails if the output duration disagrees with
    it." Per docs/design/recording.md's "what a green suite will and will
    not prove", an actual playable-file duration check needs a real
    Windows session with a monitor, which is out of pytest's reach
    entirely -- but the fault this ticket fixes lives in `_RegionCropWorker`
    (SNX-125's own diagnosis: "nothing carries timing onto the cropped
    frames"), so a real timed run *through that worker* is both reachable
    headlessly and a genuine wall-clock test, not a synthetic-timestamp
    proxy for one.

    This drives frames through the exact on_frame()/on_ready_to_send()
    sequence `_start_region` wires up for `record_seconds` of actual
    elapsed time (`time.monotonic()`, not precomputed arithmetic), stamping
    each frame from real elapsed time at the moment it arrives -- the same
    way `QScreenCapture` stamps a real one -- with roughly half coalesced
    away (two on_frame() calls per on_ready_to_send(), same as
    TestRegionCropWorker's coalescing tests). It then asserts the timing
    carried by what actually reached the encoder reconstructs the *actual*
    wall-clock interval that really elapsed, not a target duration assumed
    in advance. A worker that dropped timing (today's pre-fix code) would
    hand every surviving frame the default, untimed -1/-1, and this would
    fail loudly instead of passing on arithmetic alone.
    """

    def test_records_for_a_fixed_wall_clock_interval(self):
        frame_input = FakeVideoFrameInput()
        worker = _RegionCropWorker(frame_input, QRect(0, 0, 2, 2))

        record_seconds = 0.5
        frame_interval_seconds = 1 / 30  # QScreenCapture's own rough cadence

        wall_clock_start = time.monotonic()
        i = 0
        while time.monotonic() - wall_clock_start < record_seconds:
            frame = _make_frame(4, 4, lambda row, col: i % 251)
            frame.setStartTime(round((time.monotonic() - wall_clock_start) * 1_000_000))
            time.sleep(frame_interval_seconds)
            frame.setEndTime(round((time.monotonic() - wall_clock_start) * 1_000_000))
            worker.on_frame(frame)
            if i % 2 == 1:  # the encoder only asks for every second arrival
                worker.on_ready_to_send()
            i += 1
        # Flush whatever's left pending -- otherwise a run that ends on an
        # even i strands the last, most-recent frame unsent and the
        # reconstructed interval undershoots the real elapsed time it
        # should be checked against.
        worker.on_ready_to_send()
        wall_clock_elapsed_us = round((time.monotonic() - wall_clock_start) * 1_000_000)

        assert frame_input.sent_frames
        first_sent = frame_input.sent_frames[0]
        last_sent = frame_input.sent_frames[-1]
        assert last_sent.endTime() - first_sent.startTime() == pytest.approx(
            wall_clock_elapsed_us, rel=0.15
        )


def _windows_backend(**overrides):
    factories = dict(
        screen_capture_factory=FakeScreenCapture,
        capture_session_factory=FakeCaptureSession,
        recorder_factory=FakeRecorder,
        video_sink_factory=FakeVideoSink,
        video_frame_input_factory=FakeVideoFrameInput,
        thread_factory=FakeThread,
        # FakeRecorder.stop() settles synchronously, so these tests don't
        # need (and shouldn't pay for) the real settle delay
        # _wait_for_stopped() uses against the actual asynchronous muxer
        # -- see that method's docstring in recording.py.
        stop_settle_seconds=0,
    )
    factories.update(overrides)
    return WindowsRecorderBackend(**factories)


class TestWindowsRecorderBackendAvailability:
    def test_available_on_windows(self, monkeypatch):
        monkeypatch.setattr(recording.sys, "platform", "win32")

        backend = WindowsRecorderBackend()

        assert backend.is_available() is True
        assert backend.unavailable_reason() is None

    def test_unavailable_elsewhere_with_a_reason(self, monkeypatch):
        monkeypatch.setattr(recording.sys, "platform", "linux")

        backend = WindowsRecorderBackend()

        assert backend.is_available() is False
        assert backend.unavailable_reason() is not None


class TestWindowsRecorderBackendFullScreenStart:
    """SNX-125 checked this path for the same "recordings play back sped
    up" fault and confirmed it needs no fix: `_start_full_screen()` wires
    `QScreenCapture` straight into the same `QMediaCaptureSession` as
    `QMediaRecorder`, with no `QVideoSink`, no `_crop_frame()`, and no
    application code at all between capture and recorder --
    `test_never_constructs_the_interception_machinery` below already
    proves that structurally. Qt's own `QVideoFrame`s carry real capture
    timestamps by construction; the region path only broke once
    `_crop_frame()` started constructing fresh, unstamped frames instead
    of passing one of Qt's own through. There's no code left on this path
    for that bug to live in, so it gets no symmetrical
    `setVideoFrameRate()` call either -- that would pay the path that
    already works for a fix aimed at the one that didn't.
    """

    PATH = "C:/tmp/snipux-recording.mp4"

    def test_wires_screen_capture_straight_to_the_recorder(self):
        video_sink_factory = Mock(wraps=FakeVideoSink)
        video_frame_input_factory = Mock(wraps=FakeVideoFrameInput)
        thread_factory = Mock(wraps=FakeThread)
        backend = _windows_backend(
            video_sink_factory=video_sink_factory,
            video_frame_input_factory=video_frame_input_factory,
            thread_factory=thread_factory,
        )

        backend.start(None, self.PATH)

        assert backend._session.screen_capture is backend._screen_capture
        assert backend._session.recorder is backend._recorder
        assert backend._screen_capture.active_calls == [True]
        assert backend._recorder.record_calls == 1

    def test_never_constructs_the_interception_machinery(self):
        # The acceptance criterion that full screen "should not pay for"
        # cropping has to be true structurally: none of the region path's
        # objects exist at all, not merely unused.
        video_sink_factory = Mock(wraps=FakeVideoSink)
        video_frame_input_factory = Mock(wraps=FakeVideoFrameInput)
        thread_factory = Mock(wraps=FakeThread)
        backend = _windows_backend(
            video_sink_factory=video_sink_factory,
            video_frame_input_factory=video_frame_input_factory,
            thread_factory=thread_factory,
        )

        backend.start(None, self.PATH)

        assert video_sink_factory.called is False
        assert video_frame_input_factory.called is False
        assert thread_factory.called is False

    def test_sets_mpeg4_h264_explicitly(self):
        backend = _windows_backend()

        backend.start(None, self.PATH)

        media_format = backend._recorder.media_format
        assert media_format.fileFormat() == recording.QMediaFormat.FileFormat.MPEG4
        assert media_format.videoCodec() == recording.QMediaFormat.VideoCodec.H264

    def test_raises_when_the_screen_capture_reports_an_error(self):
        backend = _windows_backend(
            screen_capture_factory=lambda: FakeScreenCapture(fail_message="access denied")
        )

        with pytest.raises(RuntimeError, match="access denied"):
            backend.start(None, self.PATH)

    def test_raises_when_the_recorder_reports_an_error(self):
        backend = _windows_backend(
            recorder_factory=lambda: FakeRecorder(fail_message="no codec available")
        )

        with pytest.raises(RuntimeError, match="no codec available"):
            backend.start(None, self.PATH)


class TestWindowsRecorderBackendRegionStart:
    RECT = QRectF(10, 20, 200, 100)
    PATH = "C:/tmp/snipux-recording-region.mp4"

    def test_wires_capture_to_a_sink_and_frame_input_to_the_recorder_on_separate_sessions(
        self,
    ):
        backend = _windows_backend()

        backend.start(self.RECT, self.PATH)

        capture_session = backend._capture_session
        encode_session = backend._encode_session
        assert capture_session is not encode_session
        assert capture_session.screen_capture is backend._screen_capture
        assert capture_session.video_sink is backend._video_sink
        assert capture_session.recorder is None
        assert encode_session.video_frame_input is backend._frame_input
        assert encode_session.recorder is backend._recorder
        assert encode_session.screen_capture is None

    def test_moves_the_worker_to_the_thread_and_starts_it(self):
        backend = _windows_backend()

        backend.start(self.RECT, self.PATH)

        assert backend._worker_thread.start_calls == 1

    def test_connects_frame_delivery_with_an_explicit_queued_connection(self):
        backend = _windows_backend()

        backend.start(self.RECT, self.PATH)

        [(slot, connection_type)] = backend._video_sink.videoFrameChanged.connections
        assert slot == backend._worker.on_frame
        assert connection_type == Qt.ConnectionType.QueuedConnection

        [(slot, connection_type)] = backend._frame_input.readyToSendVideoFrame.connections
        assert slot == backend._worker.on_ready_to_send
        assert connection_type == Qt.ConnectionType.QueuedConnection

    def test_raises_and_tears_down_the_thread_when_the_recorder_errors(self):
        backend = _windows_backend(
            recorder_factory=lambda: FakeRecorder(fail_message="disk full")
        )

        with pytest.raises(RuntimeError, match="disk full"):
            backend.start(self.RECT, self.PATH)

    def test_does_not_declare_a_frame_rate_before_any_frame_has_arrived(self):
        # No hardcoded 30fps (or any other nominal value): until the
        # worker has actually measured something, there's nothing genuine
        # to declare, so setVideoFrameRate() must not have been called yet.
        backend = _windows_backend()

        backend.start(self.RECT, self.PATH)

        assert backend._recorder.set_video_frame_rate_calls == []

    def test_declares_the_measured_frame_rate_once_enough_frames_have_arrived(self, qapp):
        backend = _windows_backend()
        backend.start(self.RECT, self.PATH)

        # ~15fps -- distinctly different from any nominal 30fps-class
        # default, so a test that hardcoded either the old default or an
        # unrelated guess would fail this.
        interval_us = round(1_000_000 / 15)
        sample_count = _RegionCropWorker._FRAME_RATE_SAMPLE_FRAMES
        for i in range(sample_count):
            frame = _make_frame(4, 4, lambda row, col: i % 251)
            frame.setStartTime(i * interval_us)
            frame.setEndTime((i + 1) * interval_us)
            backend._worker.on_frame(frame)

        # frame_rate_measured is delivered via an explicit QueuedConnection
        # (see _start_region) -- it lands once the event loop it's queued
        # on is pumped, same as any other cross-thread Qt signal.
        qapp.processEvents()

        assert backend._recorder.set_video_frame_rate_calls == pytest.approx(
            [15.0], rel=0.01
        )


class TestWindowsRecorderBackendStop:
    def test_full_screen_deactivates_capture_before_stopping_the_recorder(self):
        calls = []

        class OrderedScreenCapture(FakeScreenCapture):
            def setActive(self, active):
                calls.append(("screen_capture.setActive", active))
                super().setActive(active)

        class OrderedRecorder(FakeRecorder):
            def stop(self):
                calls.append(("recorder.stop", None))
                super().stop()

        backend = _windows_backend(
            screen_capture_factory=OrderedScreenCapture, recorder_factory=OrderedRecorder
        )
        backend.start(None, "C:/tmp/out.mp4")
        calls.clear()

        backend.stop()

        assert calls == [("screen_capture.setActive", False), ("recorder.stop", None)]

    def test_region_stops_capture_then_the_worker_thread_then_the_recorder(self):
        calls = []

        class OrderedScreenCapture(FakeScreenCapture):
            def setActive(self, active):
                calls.append(("screen_capture.setActive", active))
                super().setActive(active)

        class OrderedThread(FakeThread):
            def quit(self):
                calls.append(("thread.quit", None))
                super().quit()

            def wait(self):
                calls.append(("thread.wait", None))
                super().wait()

        class OrderedRecorder(FakeRecorder):
            def stop(self):
                calls.append(("recorder.stop", None))
                super().stop()

        backend = _windows_backend(
            screen_capture_factory=OrderedScreenCapture,
            thread_factory=OrderedThread,
            recorder_factory=OrderedRecorder,
        )
        backend.start(QRectF(0, 0, 100, 100), "C:/tmp/out-region.mp4")
        calls.clear()

        backend.stop()

        assert calls == [
            ("screen_capture.setActive", False),
            ("thread.quit", None),
            ("thread.wait", None),
            ("recorder.stop", None),
        ]

    def test_region_disconnects_frame_delivery_before_stopping(self):
        backend = _windows_backend()
        backend.start(QRectF(0, 0, 100, 100), "C:/tmp/out-region.mp4")
        video_sink = backend._video_sink

        backend.stop()

        assert video_sink.videoFrameChanged.connections == []

    def test_waits_for_the_recorder_to_actually_reach_stopped_state(self):
        # FakeRecorder.stop() settles synchronously, so this mainly proves
        # _wait_for_stopped() doesn't hang or raise when the state is
        # already correct -- the real assertion is that stop() calls
        # recorderState() at all rather than trusting stop() blindly.
        backend = _windows_backend()
        backend.start(None, "C:/tmp/out.mp4")

        backend.stop()  # must not raise or hang

        assert backend._recorder is None


class TestBuildWindowsRegistry:
    def test_registers_the_qt_native_backend(self):
        registry = recording.build_windows_registry()

        assert [backend.name() for backend in registry] == ["qt-native"]


class TestRegionCropRunsOffTheDeliveryThread:
    """The one test in this file that needs a real `QThread` and real Qt
    signals rather than the plain fakes above: `_FakeSignal.emit()` calls
    its slot synchronously on whatever thread calls `emit()`, which cannot
    tell a same-thread bug apart from a correctly-queued one. A real
    `pyqtSignal` with an explicit `QueuedConnection`, delivered by a real
    `QThread`'s event loop, can -- and this is exactly what the spike's
    29fps->17fps regression was about: cropping inline in the delivery
    thread's handler instead of off of it.
    """

    def test_on_frame_runs_on_the_worker_thread_not_the_calling_thread(self):
        from PyQt6.QtCore import QObject, pyqtSignal

        class RealSignalSink(QObject):
            videoFrameChanged = pyqtSignal(object)

        class RealSignalFrameInput(QObject):
            readyToSendVideoFrame = pyqtSignal()

            def __init__(self):
                super().__init__()
                self.sent_from_thread = []
                self.done = threading.Event()

            def sendVideoFrame(self, frame):
                self.sent_from_thread.append(threading.current_thread())
                self.done.set()

        sink = RealSignalSink()
        frame_input = RealSignalFrameInput()
        worker = _RegionCropWorker(frame_input, QRect(0, 0, 2, 2))
        thread = QThread()
        worker.moveToThread(thread)
        sink.videoFrameChanged.connect(worker.on_frame, Qt.ConnectionType.QueuedConnection)
        frame_input.readyToSendVideoFrame.connect(
            worker.on_ready_to_send, Qt.ConnectionType.QueuedConnection
        )
        thread.start()
        try:
            calling_thread = threading.current_thread()
            frame = _make_frame(4, 4, lambda row, col: 42)

            sink.videoFrameChanged.emit(frame)
            frame_input.readyToSendVideoFrame.emit()

            assert frame_input.done.wait(timeout=5.0), "worker never called sendVideoFrame"
            assert frame_input.sent_from_thread[0] is not calling_thread
        finally:
            thread.quit()
            thread.wait()
