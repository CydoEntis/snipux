from PyQt6.QtCore import QRectF

import pytest

import snipux.recording as recording
from snipux.recording import RecorderRegistry, RecordingBackend, RecordingError


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
