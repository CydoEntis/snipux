import re

import pytest
from PyQt6.QtCore import QPointF, QRectF, QSizeF
from PyQt6.QtGui import QGuiApplication, QImage, qRgb
from PyQt6.QtWidgets import QApplication

from snipux import app
from snipux.app import (
    AppController,
    Transport,
    build_default_registry,
    copy_image_to_clipboard,
    main,
    run_resident_app,
    save_image,
)
from snipux.capture import BackendRegistry, CaptureBackend, Frame

FILL_COLOR = qRgb(10, 20, 30)


@pytest.fixture(scope="module", autouse=True)
def qapp():
    # QGuiApplication.clipboard() needs a live application instance even
    # when no widget is ever created, and this file must not depend on
    # test_editor.py having already created one — module-scoped like
    # test_editor.py's own fixture, but independent of it.
    instance = QApplication.instance()
    if instance is None:
        instance = QApplication([])
    return instance


def make_image(size=(20, 10), fill_color=FILL_COLOR) -> QImage:
    image = QImage(*size, QImage.Format.Format_RGB32)
    image.fill(fill_color)
    return image


class FakeBackend(CaptureBackend):
    def __init__(self, backend_name, available, reason=None):
        self._name = backend_name
        self._available = available
        self._reason = reason

    def name(self):
        return self._name

    def is_available(self):
        return self._available

    def unavailable_reason(self):
        return self._reason

    def capture(self):
        raise NotImplementedError


def test_list_backends_reports_name_availability_and_reason(capsys):
    registry = BackendRegistry(
        [
            FakeBackend("qt-native", True),
            FakeBackend("grim", False, reason="not on Wayland"),
        ]
    )

    exit_code = main(["--list-backends"], registry=registry)

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "qt-native" in out
    assert "available" in out
    assert "grim" in out
    assert "not on Wayland" in out


def test_list_backends_on_empty_registry_reports_none_registered(capsys):
    registry = BackendRegistry()

    exit_code = main(["--list-backends"], registry=registry)

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "no backends registered" in out


def test_no_arguments_prints_usage_mentioning_list_backends(capsys):
    registry = BackendRegistry()

    exit_code = main([], registry=registry)

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "--list-backends" in out


def test_build_default_registry_starts_empty():
    registry = build_default_registry()

    assert registry.available() == []


def test_main_does_not_require_a_display():
    # Guards against snipux.app accidentally importing something that
    # needs a live QApplication at import time; run under
    # QT_QPA_PLATFORM=offscreen like the rest of the suite.
    exit_code = main(["--list-backends"], registry=BackendRegistry())
    assert exit_code == 0


class TestCopyImageToClipboard:
    def test_always_places_the_image_on_the_qt_clipboard(self, monkeypatch):
        monkeypatch.setattr(app.shutil, "which", lambda binary: None)
        image = make_image()

        copy_image_to_clipboard(image)

        assert QGuiApplication.clipboard().image() == image

    def test_pipes_to_wl_copy_when_present_on_path(self, monkeypatch):
        monkeypatch.setattr(app.shutil, "which", lambda binary: f"/usr/bin/{binary}")
        calls = []

        def fake_run(argv, input=None, check=None):
            calls.append((argv, input))

        monkeypatch.setattr(app.subprocess, "run", fake_run)
        image = make_image(fill_color=qRgb(1, 2, 3))

        copy_image_to_clipboard(image)

        assert len(calls) == 1
        argv, piped_bytes = calls[0]
        assert argv == ["wl-copy", "--type", "image/png"]
        # Sample pixel colors rather than asserting whole-image equality:
        # QImage.__eq__ also compares format, and a PNG round trip isn't
        # guaranteed to hand back the exact same format as the RGB32
        # source on every Qt build. Matches the sampling convention used
        # throughout test_editor.py.
        round_tripped = QImage()
        assert round_tripped.loadFromData(piped_bytes)
        assert round_tripped.size() == image.size()
        assert round_tripped.pixelColor(0, 0) == image.pixelColor(0, 0)
        assert round_tripped.pixelColor(
            image.width() - 1, image.height() - 1
        ) == image.pixelColor(image.width() - 1, image.height() - 1)

    def test_does_not_raise_and_falls_back_to_qt_clipboard_when_wl_copy_absent(
        self, monkeypatch
    ):
        monkeypatch.setattr(app.shutil, "which", lambda binary: None)
        calls = []
        monkeypatch.setattr(
            app.subprocess, "run", lambda *a, **k: calls.append((a, k))
        )
        image = make_image(fill_color=qRgb(4, 5, 6))

        copy_image_to_clipboard(image)  # must not raise

        assert calls == []
        assert QGuiApplication.clipboard().image() == image

    def test_does_not_raise_when_wl_copy_binary_vanishes_before_running(self, monkeypatch):
        # A TOCTOU race: shutil.which found it, but the run itself fails.
        monkeypatch.setattr(app.shutil, "which", lambda binary: f"/usr/bin/{binary}")

        def raising_run(*args, **kwargs):
            raise FileNotFoundError("wl-copy")

        monkeypatch.setattr(app.subprocess, "run", raising_run)
        image = make_image()

        copy_image_to_clipboard(image)  # must not raise

        assert QGuiApplication.clipboard().image() == image


class TestSaveImage:
    def test_writes_into_the_given_directory_and_returns_the_path(self, tmp_path):
        image = make_image()

        path = save_image(image, tmp_path)

        assert path.parent == tmp_path
        assert path.exists()
        assert QImage(str(path)) == image

    def test_default_filename_derives_from_current_date_and_time(self, tmp_path):
        image = make_image()

        path = save_image(image, tmp_path)

        assert re.fullmatch(r"Screenshot from \d{4}-\d{2}-\d{2} \d{2}-\d{2}-\d{2}\.png", path.name)

    def test_default_directory_is_pictures_under_home_and_is_created(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setattr(app.Path, "home", lambda: tmp_path)
        image = make_image()

        path = save_image(image)

        assert path.parent == tmp_path / "Pictures"
        assert path.exists()


class FakeTransport(Transport):
    """In-memory `Transport`: no real sockets. Two instances wrapping the
    same mutable `state` dict model "first launch, then a second launch
    while the first is still resident" without spinning up a second
    process.
    """

    def __init__(self, state: dict):
        self._state = state

    def try_claim(self) -> bool:
        if self._state["claimed"]:
            return False
        self._state["claimed"] = True
        return True

    def send_snip_request(self) -> None:
        self._state["forwarded_requests"] += 1
        primary_on_request = self._state["primary_on_request"]
        if primary_on_request is not None:
            primary_on_request()

    def listen(self, on_request) -> None:
        self._state["primary_on_request"] = on_request


def make_transport_state() -> dict:
    return {"claimed": False, "forwarded_requests": 0, "primary_on_request": None}


class FakeCaptureBackend(CaptureBackend):
    """Unlike the module's `FakeBackend` (which always raises), this
    returns a real, small `Frame` — for exercising the actual
    capture -> overlay flow.
    """

    def __init__(self, frame: Frame):
        self._frame = frame

    def name(self):
        return "fake"

    def is_available(self):
        return True

    def capture(self):
        return self._frame


class FailingCaptureBackend(CaptureBackend):
    def name(self):
        return "failing"

    def is_available(self):
        return True

    def capture(self):
        raise RuntimeError("capture failed")


def make_capture_frame(size=(400, 200)) -> Frame:
    image = QImage(*size, QImage.Format.Format_RGB32)
    image.fill(FILL_COLOR)
    return Frame(image=image, logical_origin=QPointF(0, 0), logical_size=QSizeF(*size))


class TestTransportSingleInstance:
    def test_first_launch_claims_and_forwards_nothing(self):
        state = make_transport_state()
        transport = FakeTransport(state)

        assert transport.try_claim() is True
        assert state["forwarded_requests"] == 0

    def test_second_launch_fails_to_claim_and_forwards_a_request(self):
        state = make_transport_state()
        first = FakeTransport(state)
        second = FakeTransport(state)
        first.try_claim()

        assert second.try_claim() is False

        second.send_snip_request()
        assert state["forwarded_requests"] == 1

    def test_forwarded_request_reaches_the_primarys_listener(self):
        state = make_transport_state()
        first = FakeTransport(state)
        second = FakeTransport(state)
        first.try_claim()
        received = []
        first.listen(lambda: received.append(True))

        second.try_claim()
        second.send_snip_request()

        assert received == [True]


@pytest.fixture
def make_controller():
    """Builds `AppController`s and closes every window each one opened
    (overlays, editor, tray icon) at teardown.

    Overlay/editor windows this file shows are otherwise left dangling
    past their test: they're real (if offscreen) top-level windows sharing
    this process's single `QApplication`, and a leftover one has been
    observed to disturb mouse/focus-dependent assertions in unrelated
    tests (e.g. test_overlay.py's hover tests) that happen to run later in
    the same session.
    """
    controllers = []

    def _make(registry, transport, monitor_geometries=None):
        controller = AppController(registry, transport, monitor_geometries=monitor_geometries)
        controllers.append(controller)
        return controller

    yield _make

    for controller in controllers:
        for overlay in controller._overlays:
            overlay.close()
        if controller._editor is not None:
            controller._editor.close()
        controller._tray_icon.hide()


class TestAppControllerCapture:
    def test_start_capture_opens_one_overlay_per_monitor_and_no_editor(self, make_controller):
        registry = BackendRegistry([FakeCaptureBackend(make_capture_frame())])
        geometries = [QRectF(0, 0, 200, 200), QRectF(200, 0, 200, 200)]
        controller = make_controller(
            registry, FakeTransport(make_transport_state()), monitor_geometries=geometries
        )

        controller.start_capture()

        assert len(controller._overlays) == 2
        assert controller._editor is None

    def test_start_capture_is_a_noop_while_overlays_are_already_open(self, make_controller):
        registry = BackendRegistry([FakeCaptureBackend(make_capture_frame())])
        controller = make_controller(
            registry,
            FakeTransport(make_transport_state()),
            monitor_geometries=[QRectF(0, 0, 200, 200)],
        )
        controller.start_capture()
        first_overlays = controller._overlays

        controller.start_capture()

        assert controller._overlays is first_overlays

    def test_failed_capture_leaves_controller_idle(self, make_controller):
        registry = BackendRegistry([FailingCaptureBackend()])
        controller = make_controller(
            registry,
            FakeTransport(make_transport_state()),
            monitor_geometries=[QRectF(0, 0, 200, 200)],
        )

        controller.start_capture()

        assert controller._overlays == []
        assert controller._editor is None

    def test_confirming_a_selection_opens_exactly_one_editor_and_clears_overlays(
        self, make_controller
    ):
        registry = BackendRegistry([FakeCaptureBackend(make_capture_frame())])
        geometries = [QRectF(0, 0, 200, 200), QRectF(200, 0, 200, 200)]
        controller = make_controller(
            registry, FakeTransport(make_transport_state()), monitor_geometries=geometries
        )
        controller.start_capture()
        overlay = controller._overlays[0]

        overlay.confirmed.emit(QRectF(10, 10, 50, 50), None)

        assert controller._overlays == []
        assert controller._editor is not None

    def test_cancelling_returns_to_idle_with_no_editor(self, make_controller):
        registry = BackendRegistry([FakeCaptureBackend(make_capture_frame())])
        controller = make_controller(
            registry,
            FakeTransport(make_transport_state()),
            monitor_geometries=[QRectF(0, 0, 200, 200)],
        )
        controller.start_capture()
        overlay = controller._overlays[0]

        overlay.cancelled.emit()

        assert controller._overlays == []
        assert controller._editor is None


class TestAppControllerTrayMenu:
    def test_tray_menu_offers_snip_and_quit(self, make_controller):
        controller = make_controller(
            BackendRegistry(), FakeTransport(make_transport_state()), monitor_geometries=[]
        )

        assert controller.snip_action.text() == "Snip"
        assert controller.quit_action.text() == "Quit"

    def test_snip_action_triggers_start_capture(self, make_controller):
        # Asserts on the real effect of triggering the action, not on
        # whether start_capture() was called via a monkeypatched instance
        # attribute: the action's `triggered` signal is connected to the
        # bound method at __init__ time, so replacing
        # `controller.start_capture` afterwards would never be seen by an
        # already-connected slot.
        registry = BackendRegistry([FakeCaptureBackend(make_capture_frame())])
        controller = make_controller(
            registry,
            FakeTransport(make_transport_state()),
            monitor_geometries=[QRectF(0, 0, 200, 200)],
        )

        controller.snip_action.trigger()

        assert len(controller._overlays) == 1

    def test_quit_action_does_not_raise_with_no_running_event_loop(self, make_controller):
        # No test in this file ever calls .exec(), so QApplication.quit()
        # here is a harmless no-op rather than a process exit — this needs
        # no mocking to keep pytest alive, per PLAN.md.
        controller = make_controller(
            BackendRegistry(), FakeTransport(make_transport_state()), monitor_geometries=[]
        )

        controller.quit_action.trigger()


class TestRunResidentApp:
    """Exercises `run_resident_app()` itself, not just the two pieces it
    wires together — per REVIEW.md, `AppController`/`Transport` being
    individually tested above doesn't cover run_resident_app's own
    branching (try_claim() -> forward-and-return, vs. construct
    AppController and run the event loop). A flipped `not`, or an
    AppController built unconditionally, would pass every other test in
    this file.

    `QApplication.exec` is monkeypatched rather than genuinely entered:
    this file's `qapp` fixture shares one real QApplication across the
    whole module, so actually blocking in `.exec()` here would hang
    every test that runs after this one instead of failing this one.
    """

    def test_forwards_request_without_starting_a_second_instance(self, monkeypatch):
        # Proves the forwarding branch never reaches app.exec(): if it did
        # -- e.g. the try_claim() check inverted, or AppController built
        # regardless of the result -- this raises instead of silently
        # passing.
        def _must_not_be_called(self):
            raise AssertionError("app.exec() must not run when forwarding to an existing instance")

        monkeypatch.setattr(QApplication, "exec", _must_not_be_called)

        state = make_transport_state()
        state["claimed"] = True  # simulates a resident instance already running
        transport = FakeTransport(state)

        result = run_resident_app(registry=BackendRegistry(), transport=transport)

        assert result == 0
        assert state["forwarded_requests"] == 1
        # Nothing ever registered a listener: no AppController was built.
        assert state["primary_on_request"] is None

    def test_becomes_primary_and_runs_the_event_loop(self, monkeypatch):
        monkeypatch.setattr(QApplication, "exec", lambda self: 0)

        created = []

        class TrackingAppController(AppController):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                created.append(self)

        monkeypatch.setattr(app, "AppController", TrackingAppController)

        state = make_transport_state()
        transport = FakeTransport(state)

        try:
            result = run_resident_app(registry=BackendRegistry(), transport=transport)

            assert result == 0
            assert len(created) == 1
            # transport.listen() only happens inside AppController.__init__,
            # so this confirms the primary path actually constructed one
            # rather than just returning app.exec()'s value by coincidence.
            assert state["primary_on_request"] is not None
        finally:
            created[0]._tray_icon.hide()
