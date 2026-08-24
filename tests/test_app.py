import re

import pytest
from PyQt6.QtCore import QPointF, QRectF, QSizeF
from PyQt6.QtGui import QGuiApplication, QImage, qRgb
from PyQt6.QtNetwork import QLocalServer, QLocalSocket
from PyQt6.QtWidgets import QApplication, QSystemTrayIcon

from snipux import app
from snipux.app import (
    AppController,
    QLocalSocketTransport,
    Transport,
    build_default_geometry_provider,
    build_default_registry,
    cli,
    copy_image_to_clipboard,
    main,
    run_resident_app,
    save_image,
)
from snipux.capture import (
    BackendRegistry,
    CaptureBackend,
    Frame,
    X11WindowGeometryProvider,
    build_wayland_registry,
    build_x11_registry,
)
from snipux.overlay import GeometryProvider, OverlayWindow, UnsupportedGeometryProvider

FILL_COLOR = qRgb(10, 20, 30)


@pytest.fixture(scope="module", autouse=True)
def qapp():
    # QGuiApplication.clipboard() needs a live application instance even
    # when no widget is ever created, and this file must not depend on
    # test_overlay.py having already created one — module-scoped like
    # that file's own fixture, but independent of it.
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


class TestBuildDefaultRegistry:
    def test_returns_wayland_registry_when_session_type_is_wayland(self, monkeypatch):
        monkeypatch.setattr(app, "detect_session_type", lambda: "wayland")

        registry = build_default_registry()

        assert [b.name() for b in registry] == [
            b.name() for b in build_wayland_registry()
        ]

    def test_returns_x11_registry_when_session_type_is_x11(self, monkeypatch):
        monkeypatch.setattr(app, "detect_session_type", lambda: "x11")

        registry = build_default_registry()

        assert [b.name() for b in registry] == [b.name() for b in build_x11_registry()]

    def test_returns_both_registries_when_session_type_is_unknown(self, monkeypatch):
        # Neither registry is preferred over the other here: every backend
        # gates itself with its own is_available(), so offering both is how
        # an unrecognised session type still finds whatever is actually
        # installed instead of failing outright.
        monkeypatch.setattr(app, "detect_session_type", lambda: "unknown")

        registry = build_default_registry()

        expected = [b.name() for b in build_wayland_registry()] + [
            b.name() for b in build_x11_registry()
        ]
        assert [b.name() for b in registry] == expected


class TestBuildDefaultGeometryProvider:
    def test_returns_x11_window_geometry_provider_when_it_reports_available(
        self, monkeypatch
    ):
        # A fake subclass, not a monkeypatched detect_session_type/wmctrl
        # pair: X11WindowGeometryProvider.is_available() already has its
        # own tests in test_capture.py, so this only needs to prove
        # build_default_geometry_provider() *uses* that verdict, not
        # re-derive it.
        class AvailableProvider(X11WindowGeometryProvider):
            def is_available(self):
                return True

        monkeypatch.setattr(app, "X11WindowGeometryProvider", AvailableProvider)

        provider = build_default_geometry_provider()

        assert isinstance(provider, AvailableProvider)

    def test_returns_unsupported_geometry_provider_when_x11_provider_is_unavailable(
        self, monkeypatch
    ):
        # Covers both Wayland and "X11 without wmctrl" at once, since
        # is_available() is exactly what folds those two cases together.
        class UnavailableProvider(X11WindowGeometryProvider):
            def is_available(self):
                return False

        monkeypatch.setattr(app, "X11WindowGeometryProvider", UnavailableProvider)

        provider = build_default_geometry_provider()

        assert isinstance(provider, UnsupportedGeometryProvider)


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
        # throughout test_overlay.py.
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


class TestSnipFlag:
    def test_forwards_to_an_already_running_instance(self):
        state = make_transport_state()
        state["claimed"] = True  # simulates a resident instance already running

        exit_code = main(["--snip"], transport=FakeTransport(state))

        assert exit_code == 0
        assert state["forwarded_requests"] == 1

    def test_starts_a_resident_instance_and_shows_the_overlay_when_nothing_is_running(
        self, monkeypatch
    ):
        # Per SNX-53: install.sh binds this flag to a GNOME keybinding, and
        # nothing else ever starts a resident instance (no autostart entry),
        # so refusing here left the key dead on a fresh install and dead
        # again after every reboot -- the flag itself must now become the
        # resident instance instead of just erroring out.
        #
        # QApplication.exec is monkeypatched for the same reason
        # TestRunResidentApp's tests do: this file's qapp fixture shares one
        # real QApplication across the whole module, so actually blocking
        # in .exec() here would hang every test that runs after this one.
        monkeypatch.setattr(QApplication, "exec", lambda self: 0)
        created = []

        class TrackingAppController(AppController):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                created.append(self)

        monkeypatch.setattr(app, "AppController", TrackingAppController)
        registry = BackendRegistry([FakeCaptureBackend(make_capture_frame())])
        state = make_transport_state()  # fresh: nothing resident yet

        try:
            exit_code = main(
                ["--snip"], registry=registry, transport=FakeTransport(state)
            )

            assert exit_code == 0
            assert state["forwarded_requests"] == 0
            assert len(created) == 1
            # It stayed resident rather than a one-shot: a listener is now
            # registered, so the *next* press is forwarded to it rather than
            # this whole dance repeating and racing a second instance.
            assert state["primary_on_request"] is not None
            # And the overlay opened immediately -- the whole point of
            # pressing the key -- rather than only reaching an idle tray
            # icon that a Snip request would still have to be forwarded to.
            assert isinstance(created[0]._overlay, OverlayWindow)
        finally:
            if created and created[0]._overlay is not None:
                created[0]._overlay.close()
            if created:
                created[0]._tray_icon.hide()

    def test_reports_a_failed_capture_through_the_tray_rather_than_exiting_silently(
        self, monkeypatch
    ):
        # AC: "--snip still reports a real failure to capture rather than
        # exiting silently" -- becoming resident must not turn a genuine
        # capture failure into something indistinguishable from the key
        # doing nothing.
        monkeypatch.setattr(QApplication, "exec", lambda self: 0)
        # Forces the "tray exists" branch regardless of the machine running
        # the tests (see the equivalent note on
        # test_failed_capture_reports_it_through_the_tray_icon above); the
        # no-tray fallback for this same failure path is covered separately
        # by TestNoSystemTray below.
        monkeypatch.setattr(
            QSystemTrayIcon, "isSystemTrayAvailable", staticmethod(lambda: True)
        )
        calls = []
        monkeypatch.setattr(
            QSystemTrayIcon,
            "showMessage",
            lambda self, title, message, *a, **k: calls.append((title, message)),
        )
        created = []

        class TrackingAppController(AppController):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                created.append(self)

        monkeypatch.setattr(app, "AppController", TrackingAppController)
        registry = BackendRegistry([FailingCaptureBackend()])
        state = make_transport_state()  # fresh: nothing resident yet

        try:
            exit_code = main(
                ["--snip"], registry=registry, transport=FakeTransport(state)
            )

            assert exit_code == 0
            assert len(calls) == 1
            assert "capture failed" in calls[0][1]
            assert created[0]._overlay is None
        finally:
            if created:
                created[0]._tray_icon.hide()

    def test_mutually_exclusive_with_list_backends(self):
        with pytest.raises(SystemExit) as excinfo:
            main(["--snip", "--list-backends"])

        assert excinfo.value.code != 0


class TestQLocalSocketTransportRace:
    def test_try_claim_reports_not_primary_when_listen_loses_the_race(self, monkeypatch):
        # AC: "two presses in quick succession with nothing running produce
        # one running instance, not two." On the Linux target, two
        # near-simultaneous try_claim() calls can both pass the connect
        # probe (neither server is listening yet) and then race into
        # listen() -- Unix domain sockets make exactly one of those two
        # calls fail, per Qt's own documented contract for listen(). This
        # dev machine's QLocalServer doesn't reproduce that exclusivity
        # itself (a real cross-platform difference, not a test flake), so
        # the loser's listen() failure is simulated directly rather than
        # relied upon -- this exercises the same branch a real race on
        # Linux would take: try_claim() must report False, not True, and
        # must not leave `_server` pointing at a server that never actually
        # started listening.
        monkeypatch.setattr(QLocalSocket, "waitForConnected", lambda self, timeout: False)
        monkeypatch.setattr(QLocalServer, "listen", lambda self, name: False)
        transport = QLocalSocketTransport(f"snipux-test-race-{id(self)}")

        assert transport.try_claim() is False
        assert transport._server is None


class TestCli:
    def test_dispatches_to_main_when_given_arguments(self, monkeypatch):
        monkeypatch.setattr(app.sys, "argv", ["snipux", "--list-backends"])
        calls = []
        monkeypatch.setattr(app, "main", lambda: calls.append("main"))
        monkeypatch.setattr(
            app, "run_resident_app", lambda: calls.append("run_resident_app")
        )

        cli()

        assert calls == ["main"]

    def test_dispatches_to_run_resident_app_when_given_none(self, monkeypatch):
        monkeypatch.setattr(app.sys, "argv", ["snipux"])
        calls = []
        monkeypatch.setattr(app, "main", lambda: calls.append("main"))
        monkeypatch.setattr(
            app, "run_resident_app", lambda: calls.append("run_resident_app")
        )

        cli()

        assert calls == ["run_resident_app"]


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


class FakeGeometryProvider(GeometryProvider):
    """Stands in for `X11WindowGeometryProvider` in controller tests, so
    they can assert on *identity* (this exact instance reached the
    overlay) without depending on a real X11 session or `wmctrl`.
    """

    def is_available(self):
        return True

    def window_at(self, point):
        return None


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
    (its overlay, its tray icon) at teardown.

    Overlay windows this file shows are otherwise left dangling past their
    test: they're real (if offscreen) top-level windows sharing this
    process's single `QApplication`, and a leftover one has been observed
    to disturb mouse/focus-dependent assertions in unrelated tests (e.g.
    test_overlay.py's hover tests) that happen to run later in the same
    session.
    """
    controllers = []

    def _make(registry, transport, monitor_geometries=None, geometry_provider=None):
        controller = AppController(
            registry,
            transport,
            monitor_geometries=monitor_geometries,
            geometry_provider=geometry_provider,
        )
        controllers.append(controller)
        return controller

    yield _make

    for controller in controllers:
        if controller._overlay is not None:
            controller._overlay.close()
        controller._tray_icon.hide()


class TestAppControllerCapture:
    def test_start_capture_opens_the_overlay_window(self, make_controller):
        registry = BackendRegistry([FakeCaptureBackend(make_capture_frame())])
        geometries = [QRectF(0, 0, 200, 200), QRectF(200, 0, 200, 200)]
        controller = make_controller(
            registry, FakeTransport(make_transport_state()), monitor_geometries=geometries
        )

        controller.start_capture()

        # A single OverlayWindow spanning the whole desktop, not one Overlay
        # per monitor -- the new overlay's own capture-mode popover is what
        # picks Region/Window/Full screen/Freeform now, so nothing here
        # constructs the old editor.py window either.
        assert isinstance(controller._overlay, OverlayWindow)

    def test_start_capture_passes_the_controllers_geometry_provider_to_the_overlay(
        self, make_controller
    ):
        registry = BackendRegistry([FakeCaptureBackend(make_capture_frame())])
        provider = FakeGeometryProvider()
        controller = make_controller(
            registry,
            FakeTransport(make_transport_state()),
            monitor_geometries=[QRectF(0, 0, 200, 200)],
            geometry_provider=provider,
        )

        controller.start_capture()

        # Identity, not just type: proves the *injected* provider reached
        # the overlay rather than some default of OverlayWindow's own.
        assert controller._overlay._geometry_provider is provider

    def test_start_capture_defaults_to_the_controllers_own_geometry_provider(
        self, make_controller
    ):
        # No geometry_provider passed to make_controller: AppController
        # must have already resolved its own default (build_default_
        # geometry_provider(), typically UnsupportedGeometryProvider under
        # the test environment's non-X11 session) rather than leaving
        # OverlayWindow to fall back to a *different* default of its own --
        # the acceptance criterion is that AppController always decides
        # which provider is used, on every path.
        registry = BackendRegistry([FakeCaptureBackend(make_capture_frame())])
        controller = make_controller(
            registry,
            FakeTransport(make_transport_state()),
            monitor_geometries=[QRectF(0, 0, 200, 200)],
        )

        controller.start_capture()

        assert controller._overlay._geometry_provider is controller._geometry_provider

    def test_start_capture_passes_the_controllers_registry_to_the_overlay(self, make_controller):
        # OverlayWindow re-grabs through this same registry for its own
        # delayed capture (and Window/Full screen mode) -- an inert default
        # registry there could never actually re-capture anything.
        registry = BackendRegistry([FakeCaptureBackend(make_capture_frame())])
        controller = make_controller(
            registry,
            FakeTransport(make_transport_state()),
            monitor_geometries=[QRectF(0, 0, 200, 200)],
        )

        controller.start_capture()

        assert controller._overlay._registry is registry

    def test_start_capture_is_a_noop_while_the_overlay_is_already_open(self, make_controller):
        registry = BackendRegistry([FakeCaptureBackend(make_capture_frame())])
        controller = make_controller(
            registry,
            FakeTransport(make_transport_state()),
            monitor_geometries=[QRectF(0, 0, 200, 200)],
        )
        controller.start_capture()
        first_overlay = controller._overlay

        controller.start_capture()

        assert controller._overlay is first_overlay

    def test_start_capture_opens_a_new_overlay_once_the_previous_one_is_closed(
        self, make_controller
    ):
        # Proves the re-entrancy guard reads live visibility rather than
        # "an overlay was built at some point" -- otherwise a second Snip
        # after the first capture finished would stay stuck idle forever.
        registry = BackendRegistry([FakeCaptureBackend(make_capture_frame())])
        controller = make_controller(
            registry,
            FakeTransport(make_transport_state()),
            monitor_geometries=[QRectF(0, 0, 200, 200)],
        )
        controller.start_capture()
        first_overlay = controller._overlay
        first_overlay.close()

        controller.start_capture()

        assert controller._overlay is not first_overlay
        assert isinstance(controller._overlay, OverlayWindow)

    def test_failed_capture_leaves_controller_idle(self, make_controller):
        registry = BackendRegistry([FailingCaptureBackend()])
        controller = make_controller(
            registry,
            FakeTransport(make_transport_state()),
            monitor_geometries=[QRectF(0, 0, 200, 200)],
        )

        controller.start_capture()

        assert controller._overlay is None

    def test_failed_capture_reports_it_through_the_tray_icon(self, make_controller, monkeypatch):
        # Forces the "tray exists" branch regardless of what the machine
        # actually running the tests reports (typically False under the
        # offscreen platform tests run under) -- this test is specifically
        # about the tray-reporting path; the no-tray fallback has its own
        # test below.
        monkeypatch.setattr(
            QSystemTrayIcon, "isSystemTrayAvailable", staticmethod(lambda: True)
        )
        registry = BackendRegistry([FailingCaptureBackend()])
        controller = make_controller(
            registry,
            FakeTransport(make_transport_state()),
            monitor_geometries=[QRectF(0, 0, 200, 200)],
        )
        calls = []
        monkeypatch.setattr(
            controller._tray_icon, "showMessage", lambda *args, **kwargs: calls.append(args)
        )

        controller.start_capture()

        assert len(calls) == 1
        # The message body carries what actually failed, not a generic
        # "something went wrong" -- CaptureError.__str__ already collects
        # every backend's own failure per CLAUDE.md's "must not stop the
        # next one" rule, so surfacing it here is free.
        assert "failing" in calls[0][1]
        assert "capture failed" in calls[0][1]

        # The tray icon (and thus the resident process) is still alive and
        # usable after the failure, not torn down by it.
        assert controller.snip_action.text() == "Snip"
        controller.start_capture()  # must not raise a second time


class TestAppControllerTrayMenu:
    def test_tray_menu_offers_a_single_snip_item_and_quit(self, make_controller):
        controller = make_controller(
            BackendRegistry(), FakeTransport(make_transport_state()), monitor_geometries=[]
        )

        # The old per-SelectionMode items are gone: OverlayWindow's own
        # capture-mode popover is what picks Region/Window/Full screen/
        # Freeform once the overlay is open, per the ticket.
        assert controller.snip_action.text() == "Snip"
        assert controller.quit_action.text() == "Quit"
        assert [action.text() for action in controller._tray_icon.contextMenu().actions()] == [
            "Snip",
            "Quit",
        ]

    def test_snip_action_starts_a_capture(self, make_controller):
        # Asserts on the real effect of triggering the action, not on
        # whether start_capture() was called via a monkeypatched instance
        # attribute: the action's `triggered` signal is connected at
        # __init__ time, so replacing `controller.start_capture` afterwards
        # would never be seen by an already-connected slot.
        registry = BackendRegistry([FakeCaptureBackend(make_capture_frame())])
        controller = make_controller(
            registry,
            FakeTransport(make_transport_state()),
            monitor_geometries=[QRectF(0, 0, 200, 200)],
        )

        controller.snip_action.trigger()

        assert isinstance(controller._overlay, OverlayWindow)

    def test_quit_action_does_not_raise_with_no_running_event_loop(self, make_controller):
        # No test in this file ever calls .exec(), so QApplication.quit()
        # here is a harmless no-op rather than a process exit — this needs
        # no mocking to keep pytest alive, per PLAN.md.
        controller = make_controller(
            BackendRegistry(), FakeTransport(make_transport_state()), monitor_geometries=[]
        )

        controller.quit_action.trigger()


class TestNoSystemTray:
    """SNX-54: `QSystemTrayIcon.isSystemTrayAvailable()` is what decides
    whether the tray icon is actually shown -- exercised here by faking
    both outcomes directly (per the ticket's own acceptance criterion),
    rather than depending on whatever the machine running the suite
    happens to report. That matters in practice too: under the offscreen
    platform this whole file runs under, it reports False, which is why
    the tray-reporting tests above force it True instead of relying on the
    ambient value.
    """

    def test_tray_icon_is_shown_when_a_tray_is_available(self, make_controller, monkeypatch):
        monkeypatch.setattr(
            QSystemTrayIcon, "isSystemTrayAvailable", staticmethod(lambda: True)
        )

        controller = make_controller(
            BackendRegistry(), FakeTransport(make_transport_state()), monitor_geometries=[]
        )

        assert controller._tray_icon.isVisible() is True

    def test_tray_icon_is_not_shown_when_no_tray_is_available(self, make_controller, monkeypatch):
        monkeypatch.setattr(
            QSystemTrayIcon, "isSystemTrayAvailable", staticmethod(lambda: False)
        )

        controller = make_controller(
            BackendRegistry(), FakeTransport(make_transport_state()), monitor_geometries=[]
        )

        assert controller._tray_icon.isVisible() is False

    def test_prints_once_on_stdout_when_no_tray_is_available(
        self, make_controller, monkeypatch, capsys
    ):
        monkeypatch.setattr(
            QSystemTrayIcon, "isSystemTrayAvailable", staticmethod(lambda: False)
        )

        make_controller(
            BackendRegistry(), FakeTransport(make_transport_state()), monitor_geometries=[]
        )

        out = capsys.readouterr().out
        assert out.count("No system tray") == 1
        # Must actually say how to quit, not just that there's no icon --
        # with the tray's own Quit menu item invisible, this is the only
        # place that answer is ever given to the user.
        assert "quit" in out.lower()

    def test_says_nothing_when_a_tray_is_available(self, make_controller, monkeypatch, capsys):
        monkeypatch.setattr(
            QSystemTrayIcon, "isSystemTrayAvailable", staticmethod(lambda: True)
        )

        make_controller(
            BackendRegistry(), FakeTransport(make_transport_state()), monitor_geometries=[]
        )

        out = capsys.readouterr().out
        assert "No system tray" not in out

    def test_app_still_responds_to_a_snip_request_with_no_tray_available(
        self, make_controller, monkeypatch
    ):
        monkeypatch.setattr(
            QSystemTrayIcon, "isSystemTrayAvailable", staticmethod(lambda: False)
        )
        registry = BackendRegistry([FakeCaptureBackend(make_capture_frame())])
        controller = make_controller(
            registry,
            FakeTransport(make_transport_state()),
            monitor_geometries=[QRectF(0, 0, 200, 200)],
        )

        controller.start_capture()

        assert isinstance(controller._overlay, OverlayWindow)

    def test_capture_failure_is_still_reported_on_stdout_with_no_tray_available(
        self, make_controller, monkeypatch, capsys
    ):
        # AC: "a capture failure is still reported to the user when there
        # is no tray to show a balloon message in" -- showMessage() has
        # nowhere to appear on an icon that was never shown, so this must
        # not silently swallow the failure instead.
        monkeypatch.setattr(
            QSystemTrayIcon, "isSystemTrayAvailable", staticmethod(lambda: False)
        )
        registry = BackendRegistry([FailingCaptureBackend()])
        controller = make_controller(
            registry,
            FakeTransport(make_transport_state()),
            monitor_geometries=[QRectF(0, 0, 200, 200)],
        )
        capsys.readouterr()  # discard the "no tray available" notice from __init__
        show_message_calls = []
        monkeypatch.setattr(
            controller._tray_icon,
            "showMessage",
            lambda *args, **kwargs: show_message_calls.append(args),
        )

        controller.start_capture()

        out = capsys.readouterr().out
        assert "failing" in out
        assert "capture failed" in out
        assert show_message_calls == []


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
