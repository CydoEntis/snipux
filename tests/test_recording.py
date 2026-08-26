from unittest.mock import Mock

from jeepney import HeaderFields

from PyQt6.QtCore import QRectF

import pytest

import snipux.recording as recording
from snipux.recording import (
    GnomeScreencastBackend,
    RecorderRegistry,
    RecordingBackend,
    RecordingError,
)


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

    def test_calls_screencast_area_with_rounded_geometry_and_no_options(self, monkeypatch):
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
        assert message.body == (left, top, width, height, self.PATH, {})

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

    def test_calls_screencast_with_no_geometry_and_no_options(self, monkeypatch):
        connection = _fake_connection(reply=Mock(body=(True, self.PATH)))
        monkeypatch.setattr(recording, "open_dbus_connection", lambda bus: connection)

        GnomeScreencastBackend().start(None, self.PATH)

        message = connection.send_and_get_reply.call_args[0][0]
        assert message.header.fields[HeaderFields.interface] == (
            "org.gnome.Shell.Screencast"
        )
        assert message.header.fields[HeaderFields.member] == "Screencast"
        assert message.header.fields[HeaderFields.signature] == "sa{sv}"
        assert message.body == (self.PATH, {})

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
