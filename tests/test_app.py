import ctypes
import re
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
from PyQt6.QtCore import QMimeData, QPointF, QRect, QRectF, QSize, QSizeF, Qt, QUrl
from PyQt6.QtGui import QGuiApplication, QImage, qRgb
from PyQt6.QtNetwork import QLocalServer, QLocalSocket
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QSystemTrayIcon

from conftest import skip_on_windows
from snipux import app
from snipux import overlay as overlay_module
from snipux import setup_desktop
from snipux.app import (
    AppController,
    QLocalSocketTransport,
    RecordingHud,
    Transport,
    _place_recording_hud,
    build_default_geometry_provider,
    build_default_registry,
    cli,
    copy_file_to_clipboard,
    copy_image_to_clipboard,
    finish_recording,
    main,
    run_resident_app,
    save_image,
)
from snipux.capture import (
    XwininfoWindowGeometryProvider,
    BackendRegistry,
    CaptureBackend,
    Frame,
    WindowsWindowGeometryProvider,
    X11WindowGeometryProvider,
)
from snipux.overlay import GeometryProvider, OverlayWindow, UnsupportedGeometryProvider
from snipux.platform import windows as windows_platform
from snipux.recording import RecorderRegistry, RecordingBackend, RecordingError
from snipux.settings import SettingsDialog

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


@pytest.fixture(autouse=True)
def _assume_setup_already_ran(monkeypatch):
    """Default every test in this file to "a previous launch already ran
    desktop integration" (SNX-95), the same safe state a real second launch
    finds itself in via `setup_desktop.load_setup_complete()`.

    Without this, any test that lets `main(["--snip"])`/`run_resident_app()`
    actually become the resident instance -- TestSnipFlag's `--snip` tests,
    for instance, which build a real (not stubbed) `AppController` -- would
    also reach `AppController.run_first_launch_setup()` for real, which
    writes actual OS state (this developer's own `~/.config/snipux/
    config.json`, `.desktop`/autostart files, a real GNOME/Windows shortcut
    bind). Autouse so that stays true by default everywhere, not just in
    the places already known to need it; `TestRunFirstLaunchSetup` and
    `TestRunResidentApp`'s own first-launch test override this per-test to
    exercise the real "nothing has run yet" branch.
    """
    monkeypatch.setattr(app.setup_desktop, "load_setup_complete", lambda cd=None: True)


@pytest.fixture(autouse=True)
def _recordings_land_in_a_temp_folder(monkeypatch, tmp_path):
    """A stopped recording now lands somewhere real (recording.md ticket
    9's `_land_recording`) -- default that destination to pytest's own
    `tmp_path` rather than this developer's actual `~/Pictures/snipux`,
    the same isolation `_assume_setup_already_ran` above gives
    `run_first_launch_setup`. Tests that care about the landed path
    override this back with their own explicit folder.
    """
    monkeypatch.setattr(app.setup_desktop, "load_save_folder", lambda cd=None: tmp_path)


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
    """SNX-86: `build_default_registry()` no longer branches on session type
    itself -- it asks `platform.current.build_capture_registry()`. The
    session-type-driven Wayland/X11/both selection this used to do directly
    is now `capture.build_linux_registry()`'s own behaviour (what
    `platform.linux.LinuxPlatform.build_capture_registry()` forwards to),
    covered by `TestBuildLinuxRegistry` in test_capture.py -- this only
    needs to prove the delegation happens.
    """

    def test_delegates_to_the_current_platforms_build_capture_registry(self, monkeypatch):
        sentinel = BackendRegistry([FakeBackend("sentinel", True)])
        monkeypatch.setattr(
            app.platform.current, "build_capture_registry", lambda: sentinel
        )

        assert build_default_registry() is sentinel


class TestBuildDefaultGeometryProvider:
    # Every test below runs on whatever OS actually hosts the test suite
    # (this repo's own CI runs both Windows and an Ubuntu VM, per
    # CLAUDE.md), so `WindowsWindowGeometryProvider`'s *real*
    # `is_available()` -- a bare `sys.platform == "win32"` check, no
    # monkeypatching required to make it answer True on an actual Windows
    # host -- would otherwise win the X11/Xwininfo tests below out from
    # under them whenever the suite happens to run on Windows. Forcing it
    # unavailable here is what keeps those tests' verdicts about X11/xwininfo
    # priority, not about which OS happened to run them.
    def _force_windows_provider_unavailable(self, monkeypatch):
        class NoWindows(WindowsWindowGeometryProvider):
            def is_available(self):
                return False

        monkeypatch.setattr(app, "WindowsWindowGeometryProvider", NoWindows)

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

    def test_falls_back_to_xwininfo_when_wmctrl_is_missing(self, monkeypatch):
        # The reported bug: wmctrl is not installed by default on Ubuntu, so
        # Window mode silently reverted to Region on a stock desktop.
        class NoWmctrl(X11WindowGeometryProvider):
            def is_available(self):
                return False

        class HasXwininfo(XwininfoWindowGeometryProvider):
            def is_available(self):
                return True

        monkeypatch.setattr(app, "X11WindowGeometryProvider", NoWmctrl)
        monkeypatch.setattr(app, "XwininfoWindowGeometryProvider", HasXwininfo)

        assert isinstance(build_default_geometry_provider(), HasXwininfo)

    def test_falls_back_to_windows_when_neither_x11_tool_is_available(
        self, monkeypatch
    ):
        # SNX-90: a win32 host has no wmctrl/xwininfo at all, but it isn't
        # stuck with UnsupportedGeometryProvider the way a bare Wayland
        # session is -- EnumWindows can always answer there.
        class NoWmctrl(X11WindowGeometryProvider):
            def is_available(self):
                return False

        class NoXwininfo(XwininfoWindowGeometryProvider):
            def is_available(self):
                return False

        class AvailableWindows(WindowsWindowGeometryProvider):
            def is_available(self):
                return True

        monkeypatch.setattr(app, "X11WindowGeometryProvider", NoWmctrl)
        monkeypatch.setattr(app, "XwininfoWindowGeometryProvider", NoXwininfo)
        monkeypatch.setattr(app, "WindowsWindowGeometryProvider", AvailableWindows)

        provider = build_default_geometry_provider()

        assert isinstance(provider, AvailableWindows)

    def test_returns_unsupported_geometry_provider_when_none_is_available(
        self, monkeypatch
    ):
        # Wayland, or an X11 session with neither tool and not on Windows
        # either: window mode degrades to plain rectangle dragging rather
        # than pretending to work.
        class UnavailableProvider(X11WindowGeometryProvider):
            def is_available(self):
                return False

        class NoXwininfo(XwininfoWindowGeometryProvider):
            def is_available(self):
                return False

        monkeypatch.setattr(app, "XwininfoWindowGeometryProvider", NoXwininfo)
        monkeypatch.setattr(app, "X11WindowGeometryProvider", UnavailableProvider)
        self._force_windows_provider_unavailable(monkeypatch)

        provider = build_default_geometry_provider()

        assert isinstance(provider, UnsupportedGeometryProvider)


def test_main_does_not_require_a_display():
    # Guards against snipux.app accidentally importing something that
    # needs a live QApplication at import time; run under
    # QT_QPA_PLATFORM=offscreen like the rest of the suite.
    exit_code = main(["--list-backends"], registry=BackendRegistry())
    assert exit_code == 0


class TestLoadAppIcon:
    """SNX-81: `AppController._build_icon`'s old drawn-placeholder `QIcon`
    is now `load_app_icon()`, loading the vendored `design/logo/
    snipux-<size>.png` files instead.
    """

    def test_uses_the_vendored_artwork(self):
        icon = app.load_app_icon()

        assert not icon.isNull()

    def test_every_vendored_logo_size_is_added(self):
        # Exercises the real, checked-in assets (not a synthetic fixture)
        # so a corrupt or renamed logo size fails a test instead of only
        # surfacing as a blurry tray icon at runtime.
        expected_sizes = set()
        for path in app._LOGO_DIR.glob("snipux-*.png"):
            match = re.fullmatch(r"snipux-(\d+)\.png", path.name)
            if match:
                size = int(match.group(1))
                expected_sizes.add(QSize(size, size))
        assert expected_sizes, "expected at least one vendored logo size"

        icon = app.load_app_icon()

        assert set(icon.availableSizes()) == expected_sizes

    def test_falls_back_to_a_placeholder_when_the_logo_directory_is_missing(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setattr(app, "_LOGO_DIR", tmp_path / "no-such-dir")

        icon = app.load_app_icon()  # must not raise

        assert not icon.isNull()

    def test_falls_back_to_a_placeholder_when_every_vendored_file_is_corrupt(
        self, monkeypatch, tmp_path
    ):
        (tmp_path / "snipux-32.png").write_bytes(b"not a real png")
        monkeypatch.setattr(app, "_LOGO_DIR", tmp_path)

        icon = app.load_app_icon()  # must not raise

        assert not icon.isNull()


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


class TestCopyFileToClipboard:
    """These tests prove the shape of the `QMimeData` this code builds on
    an offscreen Qt clipboard (right URLs, right GNOME flavour, right bytes
    piped to `wl-copy`). They cannot prove a real Nautilus/Explorer/Slack
    window actually paints a file icon -- per docs/design/recording.md,
    that half needs a human on a real GNOME session and a real Windows box.
    """

    def test_puts_a_file_url_on_the_qt_clipboard(self, monkeypatch, tmp_path):
        monkeypatch.setattr(app.shutil, "which", lambda binary: None)
        path = tmp_path / "clip.mp4"
        path.write_bytes(b"fake video bytes")

        copy_file_to_clipboard(path)

        urls = QGuiApplication.clipboard().mimeData().urls()
        assert urls == [QUrl.fromLocalFile(str(path))]

    def test_sets_the_gnome_special_flavour_alongside_the_url_list(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setattr(app.shutil, "which", lambda binary: None)
        # A space in the directory name is what forces percent-encoding,
        # and it is not a corner case: the default filename pattern
        # ("Screenshot from %Y-%m-%d %H-%M-%S") always contains spaces, so
        # every recording copied to the clipboard goes through this.
        directory = tmp_path / "a b"
        directory.mkdir()
        path = directory / "clip.mp4"
        path.write_bytes(b"fake video bytes")

        copy_file_to_clipboard(path)

        mime = QGuiApplication.clipboard().mimeData()
        payload = bytes(mime.data("x-special/gnome-copied-files").data())
        operation, _, uri = payload.partition(b"\n")

        assert operation == b"copy"
        # Spelled out rather than recomputed through the same QUrl call the
        # implementation makes. Asserting `toString()` against `toString()`
        # is exactly what let the unescaped form ship: the test agreed with
        # the bug. A URI with a raw space in it is not a URI.
        assert b" " not in uri
        assert uri.endswith(b"/a%20b/clip.mp4")
        # Both flavours ride on one QMimeData and must name the same file
        # the same way.
        assert uri == bytes(mime.urls()[0].toEncoded())

    def test_pipes_the_uri_list_to_wl_copy_when_present_on_path(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setattr(app.shutil, "which", lambda binary: f"/usr/bin/{binary}")
        calls = []

        def fake_run(argv, input=None, check=None):
            calls.append((argv, input))

        monkeypatch.setattr(app.subprocess, "run", fake_run)
        directory = tmp_path / "a b"
        directory.mkdir()
        path = directory / "clip.mp4"
        path.write_bytes(b"fake video bytes")

        copy_file_to_clipboard(path)

        assert len(calls) == 1
        argv, piped_bytes = calls[0]
        assert argv == ["wl-copy", "--type", "text/uri-list"]
        # Same rule as the GNOME flavour, and spelled out for the same
        # reason -- text/uri-list carries URIs, so this one is escaped too.
        assert b" " not in piped_bytes
        assert piped_bytes.endswith(b"/a%20b/clip.mp4\n")

    def test_does_not_raise_when_wl_copy_binary_vanishes_before_running(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setattr(app.shutil, "which", lambda binary: f"/usr/bin/{binary}")

        def raising_run(*args, **kwargs):
            raise FileNotFoundError("wl-copy")

        monkeypatch.setattr(app.subprocess, "run", raising_run)
        path = tmp_path / "clip.mp4"
        path.write_bytes(b"fake video bytes")

        copy_file_to_clipboard(path)  # must not raise

        urls = QGuiApplication.clipboard().mimeData().urls()
        assert urls == [QUrl.fromLocalFile(str(path))]

    def test_does_not_raise_and_falls_back_to_qt_clipboard_when_wl_copy_absent(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setattr(app.shutil, "which", lambda binary: None)
        calls = []
        monkeypatch.setattr(
            app.subprocess, "run", lambda *a, **k: calls.append((a, k))
        )
        path = tmp_path / "clip.mp4"
        path.write_bytes(b"fake video bytes")

        copy_file_to_clipboard(path)  # must not raise

        assert calls == []
        urls = QGuiApplication.clipboard().mimeData().urls()
        assert urls == [QUrl.fromLocalFile(str(path))]


class TestFinishRecording:
    """As with `TestCopyFileToClipboard`, these prove the clipboard/no-op
    dispatch this code performs, not that a human sees a pasted file on a
    real desktop -- see that class's docstring.
    """

    def test_instant_copies_the_finished_file_to_the_clipboard(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setattr(app.shutil, "which", lambda binary: None)
        path = tmp_path / "clip.mp4"
        path.write_bytes(b"fake video bytes")

        finish_recording(path, "instant")

        urls = QGuiApplication.clipboard().mimeData().urls()
        assert urls == [QUrl.fromLocalFile(str(path))]

    def test_save_does_not_touch_the_clipboard(self, tmp_path):
        sentinel = QMimeData()
        sentinel.setText("sentinel")
        QGuiApplication.clipboard().setMimeData(sentinel)
        path = tmp_path / "clip.mp4"
        path.write_bytes(b"fake video bytes")

        finish_recording(path, "save")

        assert QGuiApplication.clipboard().mimeData().text() == "sentinel"


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

    def send_settings_request(self) -> None:
        self._state["forwarded_settings_requests"] += 1
        primary_on_settings_request = self._state["primary_on_settings_request"]
        if primary_on_settings_request is not None:
            primary_on_settings_request()

    def listen(self, on_request, on_settings_request) -> None:
        self._state["primary_on_request"] = on_request
        self._state["primary_on_settings_request"] = on_settings_request


def make_transport_state() -> dict:
    return {
        "claimed": False,
        "forwarded_requests": 0,
        "forwarded_settings_requests": 0,
        "primary_on_request": None,
        "primary_on_settings_request": None,
    }


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
        # This goes through main() -> _become_resident(), which really
        # calls install_hotkey_listener() -- a genuine RegisterHotKey
        # against the OS on Windows. Left unmocked, that call's own success
        # or failure (e.g. "already in use by another application" if a
        # real Snipux -- or a previous, still-registered test run -- holds
        # the key) shows up as a second, unrelated tray message and makes
        # this test's outcome depend on what else is running on the
        # machine. Stubbing bind_shortcut() to a routine success (see
        # TestInstallHotkeyListener's own tests for this same pattern)
        # keeps this test about the capture-failure message alone.
        monkeypatch.setattr(
            app.platform.current,
            "bind_shortcut",
            lambda shortcut=None: "Bound Control+Alt+S to start a snip.",
        )
        monkeypatch.setattr(
            app.platform.current, "registered_shortcut", "Control+Alt+S", raising=False
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


class TestSettingsFlag:
    """SNX-78: `--settings` follows the same forward-or-become-resident
    shape `TestSnipFlag` above already covers for `--snip`, and for the
    same reason -- it is the way in on a machine with no tray icon to
    click "Settings..." on (stock GNOME without the AppIndicator
    extension).
    """

    def test_forwards_to_an_already_running_instance(self):
        state = make_transport_state()
        state["claimed"] = True  # simulates a resident instance already running

        exit_code = main(["--settings"], transport=FakeTransport(state))

        assert exit_code == 0
        assert state["forwarded_settings_requests"] == 1
        # The *settings* request forwarded, never a snip -- a machine with
        # no tray asking to configure snipux must not also take a capture.
        assert state["forwarded_requests"] == 0

    def test_starts_a_resident_instance_and_opens_settings_when_nothing_is_running(
        self, monkeypatch
    ):
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
                ["--settings"], registry=registry, transport=FakeTransport(state)
            )

            assert exit_code == 0
            assert state["forwarded_settings_requests"] == 0
            assert len(created) == 1
            # Stayed resident, same as --snip's own equivalent test -- the
            # *next* --settings (or --snip) is forwarded to it rather than
            # this whole dance repeating and racing a second instance.
            assert state["primary_on_settings_request"] is not None
            # And Settings opened immediately, the whole point of running
            # the flag, rather than only reaching an idle tray a Settings
            # request would still have to be forwarded to.
            assert isinstance(created[0]._settings, SettingsDialog)
            # No overlay: --settings must not also start a capture.
            assert created[0]._overlay is None
        finally:
            if created and created[0]._settings is not None:
                created[0]._settings.close()
            if created:
                created[0]._tray_icon.hide()

    def test_mutually_exclusive_with_snip(self):
        with pytest.raises(SystemExit) as excinfo:
            main(["--settings", "--snip"])

        assert excinfo.value.code != 0

    def test_mutually_exclusive_with_list_backends(self):
        with pytest.raises(SystemExit) as excinfo:
            main(["--settings", "--list-backends"])

        assert excinfo.value.code != 0


class TestSetupFlag:
    """SNX-73/SNX-92: `--setup` dispatches to
    `platform.current.install_desktop_integration()` rather than building a
    registry or touching a display -- the desktop entry, autostart entry,
    and shortcut it installs have nothing to do with capture backends.
    Mocked on `platform.current` itself (whichever `Platform` the host this
    suite runs on resolves to -- `LinuxPlatform`/`WindowsPlatform`, both
    real per CLAUDE.md's "development happens on Windows and in an Ubuntu
    VM"), not on `setup_desktop.run_setup()`, which only `LinuxPlatform`
    calls; `install_desktop_integration()`'s own real behaviour is covered
    directly in test_setup_desktop.py (Linux) and test_platform.py
    (Windows) -- this only proves main() reaches it.
    """

    def test_dispatches_to_install_desktop_integration_and_returns_its_exit_code(
        self, monkeypatch
    ):
        calls = []
        monkeypatch.setattr(
            app.platform.current,
            "install_desktop_integration",
            lambda **kwargs: calls.append("called") or 0,
        )

        exit_code = main(["--setup"])

        assert exit_code == 0
        assert calls == ["called"]

    def test_propagates_a_nonzero_exit_code_from_install_desktop_integration(self, monkeypatch):
        monkeypatch.setattr(
            app.platform.current, "install_desktop_integration", lambda **kwargs: 1
        )

        assert main(["--setup"]) == 1

    def test_does_not_build_a_registry(self, monkeypatch):
        # A registry built here would mean --setup pays for probing real
        # capture backends it has no use for; build_default_registry raising
        # proves it's never called on this path.
        monkeypatch.setattr(
            app.platform.current, "install_desktop_integration", lambda **kwargs: 0
        )

        def must_not_be_called():
            raise AssertionError("build_default_registry() must not run for --setup")

        monkeypatch.setattr(app, "build_default_registry", must_not_be_called)

        main(["--setup"])

    def test_mutually_exclusive_with_snip(self):
        with pytest.raises(SystemExit) as excinfo:
            main(["--setup", "--snip"])

        assert excinfo.value.code != 0

    def test_mutually_exclusive_with_list_backends(self):
        with pytest.raises(SystemExit) as excinfo:
            main(["--setup", "--list-backends"])

        assert excinfo.value.code != 0


class TestRemoveFlag:
    """SNX-83/SNX-92: `--remove` dispatches to
    `platform.current.remove_desktop_integration()` the same way `--setup`
    dispatches to `install_desktop_integration()` -- undoing the desktop
    entry, autostart entry, installed icons, and shortcut has nothing to do
    with capture backends either. `remove_desktop_integration()`'s own
    behaviour is covered directly in test_setup_desktop.py (Linux) and
    test_platform.py (Windows); this only proves main() reaches it.
    """

    def test_dispatches_to_remove_desktop_integration_and_returns_its_exit_code(
        self, monkeypatch
    ):
        calls = []
        monkeypatch.setattr(
            app.platform.current,
            "remove_desktop_integration",
            lambda: calls.append("called") or 0,
        )

        exit_code = main(["--remove"])

        assert exit_code == 0
        assert calls == ["called"]

    def test_propagates_a_nonzero_exit_code_from_remove_desktop_integration(self, monkeypatch):
        monkeypatch.setattr(app.platform.current, "remove_desktop_integration", lambda: 1)

        assert main(["--remove"]) == 1

    def test_does_not_build_a_registry(self, monkeypatch):
        # A registry built here would mean --remove pays for probing real
        # capture backends it has no use for; build_default_registry raising
        # proves it's never called on this path.
        monkeypatch.setattr(app.platform.current, "remove_desktop_integration", lambda: 0)

        def must_not_be_called():
            raise AssertionError("build_default_registry() must not run for --remove")

        monkeypatch.setattr(app, "build_default_registry", must_not_be_called)

        main(["--remove"])

    def test_mutually_exclusive_with_setup(self):
        with pytest.raises(SystemExit) as excinfo:
            main(["--remove", "--setup"])

        assert excinfo.value.code != 0

    def test_mutually_exclusive_with_snip(self):
        with pytest.raises(SystemExit) as excinfo:
            main(["--remove", "--snip"])

        assert excinfo.value.code != 0

    def test_mutually_exclusive_with_list_backends(self):
        with pytest.raises(SystemExit) as excinfo:
            main(["--remove", "--list-backends"])

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
    """`reattach_console` (SNX-100) is stubbed out in every test here, the
    same reasoning `TestWindowsHotkeyIntegration` already applies to
    `RegisterHotKey`: it is a real Win32 call on whatever machine runs this
    suite, and cli() must reach it on every dispatch path without this
    file's own assertions depending on (or corrupting) this process's
    actual stdout/stderr.
    """

    def test_dispatches_to_main_when_given_arguments(self, monkeypatch):
        monkeypatch.setattr(app.sys, "argv", ["snipux", "--list-backends"])
        monkeypatch.setattr(app, "reattach_console", lambda: None)
        calls = []
        monkeypatch.setattr(app, "main", lambda: calls.append("main"))
        monkeypatch.setattr(
            app, "run_resident_app", lambda: calls.append("run_resident_app")
        )

        cli()

        assert calls == ["main"]

    def test_dispatches_to_run_resident_app_when_given_none(self, monkeypatch):
        monkeypatch.setattr(app.sys, "argv", ["snipux"])
        monkeypatch.setattr(app, "reattach_console", lambda: None)
        calls = []
        monkeypatch.setattr(app, "main", lambda: calls.append("main"))
        monkeypatch.setattr(
            app, "run_resident_app", lambda: calls.append("run_resident_app")
        )

        cli()

        assert calls == ["run_resident_app"]

    def test_reattaches_the_console_before_dispatching(self, monkeypatch):
        # Order matters (SNX-100's own acceptance criterion): main()/
        # run_resident_app() may print before returning, so the console
        # must already be sorted out by the time either one is reached.
        monkeypatch.setattr(app.sys, "argv", ["snipux"])
        calls = []
        monkeypatch.setattr(app, "reattach_console", lambda: calls.append("reattach"))
        monkeypatch.setattr(app, "main", lambda: calls.append("main"))
        monkeypatch.setattr(
            app, "run_resident_app", lambda: calls.append("run_resident_app")
        )

        cli()

        assert calls == ["reattach", "run_resident_app"]


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
        first.listen(lambda: received.append(True), lambda: None)

        second.try_claim()
        second.send_snip_request()

        assert received == [True]

    def test_forwarded_settings_request_reaches_the_primarys_listener(self):
        state = make_transport_state()
        first = FakeTransport(state)
        second = FakeTransport(state)
        first.try_claim()
        received = []
        first.listen(lambda: None, lambda: received.append(True))

        second.try_claim()
        second.send_settings_request()

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

    def _make(
        registry,
        transport,
        monitor_geometries=None,
        geometry_provider=None,
        recorder_registry=None,
        disk_usage=shutil.disk_usage,
    ):
        controller = AppController(
            registry,
            transport,
            monitor_geometries=monitor_geometries,
            geometry_provider=geometry_provider,
            recorder_registry=recorder_registry,
            disk_usage=disk_usage,
        )
        controllers.append(controller)
        return controller

    yield _make

    for controller in controllers:
        if controller._overlay is not None:
            controller._overlay.close()
        # Same leak controllers gives overlays: a test that starts a
        # recording and never explicitly stops it would otherwise leave a
        # real top-level widget (the HUD) and a live QTimer behind for
        # whatever test runs next.
        if controller._recording_hud is not None:
            controller._recording_hud.close()
        if controller._recording_elapsed_timer is not None:
            controller._recording_elapsed_timer.stop()
        if controller._recording_delay_timer is not None:
            controller._recording_delay_timer.stop()
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

    def test_start_capture_reads_the_hint_bar_preference_fresh_from_settings(
        self, make_controller, monkeypatch
    ):
        # AC (SNX-78): toggling the hint-bar preference in Settings takes
        # effect on the very next snip, the same "read fresh, not cached at
        # startup" rule `_on_captured`'s own `load_review_window()` call
        # already follows for the after-capture behaviour.
        monkeypatch.setattr(app.setup_desktop, "load_hints_enabled", lambda cd=None: True)
        registry = BackendRegistry([FakeCaptureBackend(make_capture_frame())])
        controller = make_controller(
            registry,
            FakeTransport(make_transport_state()),
            monitor_geometries=[QRectF(0, 0, 200, 200)],
        )

        controller.start_capture()

        assert controller._overlay.hints_enabled is True

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


class TestPlaceRecordingHud:
    """SNX-123 ticket 8, reshaped by the recording-flow fixes:
    `_place_recording_hud` is a pure function of plain `QRectF`/`QSize`
    values, unit-tested directly the same way test_recording.py tests
    `_rect_to_screen_pixels` -- no `QApplication` widgets involved.
    """

    HUD_SIZE = QSize(220, 44)
    SCREEN = QRectF(0, 0, 1000, 800)

    def test_sits_top_centre_of_the_screen(self):
        rect = QRectF(100, 300, 200, 150)

        result = _place_recording_hud(rect, [self.SCREEN], self.HUD_SIZE)

        assert result is not None
        assert result.top() == round(self.SCREEN.top() + 12.0)
        assert result.left() == round(self.SCREEN.center().x() - 220 / 2)

    def test_the_position_does_not_depend_on_where_the_region_is(self):
        # The rule this replaced centred the pill on an edge of the
        # *region*, so a region in the middle of the screen put the pill
        # in the middle of the screen, with nothing tying its position to
        # anywhere the user could predict.
        first = _place_recording_hud(
            QRectF(10, 500, 40, 40), [self.SCREEN], self.HUD_SIZE
        )
        second = _place_recording_hud(
            QRectF(700, 200, 250, 300), [self.SCREEN], self.HUD_SIZE
        )

        assert first is not None
        assert first == second

    def test_a_full_screen_recording_still_gets_somewhere_to_arm_from(self):
        # This returned None before, which made "no pill in a full-screen
        # recording" true by construction. Arming needs a visible Start for
        # a full-screen recording as much as any other, so that rule moved
        # to `_start_recording_ui`, which takes the pill down at the moment
        # recording actually begins.
        result = _place_recording_hud(None, [self.SCREEN], self.HUD_SIZE)

        assert result is not None
        assert result.top() == round(self.SCREEN.top() + 12.0)

    def test_moves_below_a_region_that_covers_the_top_of_the_screen(self):
        rect = QRectF(0, 0, 1000, 200)

        result = _place_recording_hud(rect, [self.SCREEN], self.HUD_SIZE)

        assert result is not None
        assert result.top() == round(rect.bottom() + 12.0)

    def test_never_overlaps_the_recorded_area(self):
        # The pill sitting inside the recording is the one thing top-centre
        # placement could newly get wrong, so it is checked across the
        # shapes that reach for the fallback rather than just one.
        for rect in (
            QRectF(0, 0, 1000, 200),
            QRectF(300, 0, 400, 100),
            QRectF(0, 0, 600, 60),
            QRectF(450, 30, 100, 500),
            QRectF(100, 300, 200, 150),
        ):
            result = _place_recording_hud(rect, [self.SCREEN], self.HUD_SIZE)
            assert result is not None, rect
            assert not QRectF(result).intersects(rect), rect

    def test_none_when_the_region_leaves_nowhere_to_put_it(self):
        rect = QRectF(0, 0, 1000, 800)

        assert _place_recording_hud(rect, [self.SCREEN], self.HUD_SIZE) is None

    def test_none_without_any_monitor_geometry(self):
        assert _place_recording_hud(QRectF(0, 0, 10, 10), [], self.HUD_SIZE) is None

    def test_uses_the_monitor_the_recording_is_actually_on(self):
        # Including one at negative coordinates, the arrangement this
        # project already tests against real multi-monitor hardware.
        left_monitor = QRectF(-1920, 0, 1920, 1080)
        rect = QRectF(-1500, 400, 300, 200)

        result = _place_recording_hud(
            rect, [self.SCREEN, left_monitor], self.HUD_SIZE
        )

        assert result is not None
        assert result.top() == round(left_monitor.top() + 12.0)
        assert result.left() == round(left_monitor.center().x() - 220 / 2)

    def test_falls_back_to_the_union_for_a_region_between_two_monitors(self):
        # A centre landing in the gap between two non-adjacent monitors
        # belongs to neither; the union is the only rect that contains it.
        right_monitor = QRectF(1200, 0, 1000, 800)
        rect = QRectF(1000, 100, 200, 200)

        result = _place_recording_hud(
            rect, [self.SCREEN, right_monitor], self.HUD_SIZE
        )

        assert result is not None
        union = self.SCREEN.united(right_monitor)
        assert result.left() == round(union.center().x() - 220 / 2)


class FakeRecordingBackend(RecordingBackend):
    """Mirrors test_recording.py's own `FakeBackend` -- a small
    `RecordingBackend` implementation for exercising `AppController`'s
    recording seam without a real recorder, recording every `start()` call
    it receives rather than mocking the ABC.
    """

    def __init__(self, backend_name="fake", available=True, start_error=None, stop_error=None):
        self._name = backend_name
        self._available = available
        self._start_error = start_error
        self._stop_error = stop_error
        self.start_calls = []
        self.stop_calls = []

    def name(self):
        return self._name

    def is_available(self):
        return self._available

    def unavailable_reason(self):
        return None

    def start(self, rect, path):
        self.start_calls.append((rect, path))
        if self._start_error is not None:
            raise self._start_error
        # Honours the path it was handed, as WindowsRecorderBackend does;
        # GNOME's renaming is exercised in test_recording.py instead.
        return path

    def stop(self):
        self.stop_calls.append(True)
        if self._stop_error is not None:
            raise self._stop_error


def _record(controller, rect, delay="Off", after=None):
    """Arm a recording and press Start, the way a user does.

    Committing a selection only *arms* a recording (the pill's "Start
    recording" state); these tests are about what happens once one is
    genuinely running, so they go through both halves. The tests that are
    specifically about the gap between the two call the two methods
    directly instead.
    """
    if after is None:
        controller._on_recording_requested(rect, delay)
    else:
        controller._on_recording_requested(rect, delay, after)
    controller._begin_armed_recording()


class TestAppControllerRecording:
    """SNX-122: `_on_recording_requested` is app.py's half of "committing a
    selection on the recording side ... starts the recorder on that rect"
    -- `OverlayWindow._commit_selection`'s record branch is what calls it,
    covered from the overlay.py side in test_overlay.py's own
    `TestCommitToRecord`.
    """

    def test_committing_arms_and_the_start_press_begins_an_off_delay_recording(
        self, make_controller
    ):
        backend = FakeRecordingBackend()
        registry = RecorderRegistry([backend])
        controller = make_controller(
            BackendRegistry([FakeCaptureBackend(make_capture_frame())]),
            FakeTransport(make_transport_state()),
            recorder_registry=registry,
        )
        rect = QRectF(10, 20, 300, 200)

        controller._on_recording_requested(rect, "Off")

        # Committing a selection only arms: nothing is recording yet, and
        # the pill is offering to start. This gap is the whole point --
        # there used to be no moment between choosing and recording, so
        # every recording opened with the user getting ready.
        assert backend.start_calls == []
        assert controller._armed_recording is not None
        assert controller._recording_hud.state() == app.RecordingHud.ARMED

        controller._begin_armed_recording()

        assert len(backend.start_calls) == 1
        started_rect, path = backend.start_calls[0]
        assert started_rect == rect
        # ticket 8's own seam: the backend that actually started, held
        # alongside the path it's writing to, for a later `.stop()`.
        # ticket 9 adds the third element: `after`, defaulted here since
        # this call only passed (rect, delay) -- see `_on_recording_requested`.
        assert controller._active_recording == (
            backend,
            path,
            app.design.tokens.RECORD_AFTER_DEFAULT,
        )

    def test_a_non_off_delay_counts_down_on_the_pill_before_starting(
        self, make_controller
    ):
        backend = FakeRecordingBackend()
        registry = RecorderRegistry([backend])
        controller = make_controller(
            BackendRegistry([FakeCaptureBackend(make_capture_frame())]),
            FakeTransport(make_transport_state()),
            recorder_registry=registry,
        )

        controller._on_recording_requested(QRectF(0, 0, 100, 100), "3s")

        # Armed, but not counting: the countdown belongs to Start, not to
        # committing the selection.
        assert backend.start_calls == []
        assert controller._countdown_timer is None

        controller._begin_armed_recording()

        assert backend.start_calls == []
        assert controller._countdown_timer is not None
        assert controller._recording_hud.state() == app.RecordingHud.COUNTING

        # Mirrors test_overlay.py's own `overlay._delay_timer.timeout.emit()`
        # convention for SNX-50's delayed capture -- firing the signal
        # directly rather than waiting out 3 real seconds.
        timer = controller._countdown_timer
        for _ in range(3):
            timer.timeout.emit()

        assert len(backend.start_calls) == 1
        assert controller._recording_hud.state() == app.RecordingHud.RECORDING

    def test_a_recording_error_is_reported_through_the_tray(
        self, make_controller, monkeypatch
    ):
        monkeypatch.setattr(
            QSystemTrayIcon, "isSystemTrayAvailable", staticmethod(lambda: True)
        )
        backend = FakeRecordingBackend(start_error=RuntimeError("boom"))
        registry = RecorderRegistry([backend])
        controller = make_controller(
            BackendRegistry([FakeCaptureBackend(make_capture_frame())]),
            FakeTransport(make_transport_state()),
            recorder_registry=registry,
        )
        calls = []
        monkeypatch.setattr(
            controller._tray_icon, "showMessage", lambda *args, **kwargs: calls.append(args)
        )

        _record(controller, QRectF(0, 0, 100, 100), "Off")

        assert len(calls) == 1
        assert "boom" in calls[0][1]
        assert controller._active_recording is None

    def test_a_recording_error_prints_on_stdout_with_no_tray_available(
        self, make_controller, monkeypatch, capsys
    ):
        monkeypatch.setattr(
            QSystemTrayIcon, "isSystemTrayAvailable", staticmethod(lambda: False)
        )
        backend = FakeRecordingBackend(start_error=RuntimeError("boom"))
        registry = RecorderRegistry([backend])
        controller = make_controller(
            BackendRegistry([FakeCaptureBackend(make_capture_frame())]),
            FakeTransport(make_transport_state()),
            recorder_registry=registry,
        )

        _record(controller, QRectF(0, 0, 100, 100), "Off")

        assert "boom" in capsys.readouterr().out

    def test_a_recording_error_removes_the_placeholder_temp_file(self, make_controller):
        backend = FakeRecordingBackend(start_error=RuntimeError("boom"))
        registry = RecorderRegistry([backend])
        controller = make_controller(
            BackendRegistry([FakeCaptureBackend(make_capture_frame())]),
            FakeTransport(make_transport_state()),
            recorder_registry=registry,
        )

        _record(controller, QRectF(0, 0, 100, 100), "Off")

        # The path handed to `backend.start` is the placeholder tempfile
        # `_on_recording_requested` creates before calling it -- a start
        # that never happened shouldn't leave an empty file behind for
        # ticket 9 to trip over later.
        assert len(backend.start_calls) == 1
        _, path = backend.start_calls[0]
        assert not Path(path).exists()

    def test_a_second_commit_to_record_while_one_is_counting_down_is_refused(
        self, make_controller, monkeypatch
    ):
        monkeypatch.setattr(
            QSystemTrayIcon, "isSystemTrayAvailable", staticmethod(lambda: True)
        )
        backend = FakeRecordingBackend()
        registry = RecorderRegistry([backend])
        controller = make_controller(
            BackendRegistry([FakeCaptureBackend(make_capture_frame())]),
            FakeTransport(make_transport_state()),
            recorder_registry=registry,
        )
        calls = []
        monkeypatch.setattr(
            controller._tray_icon, "showMessage", lambda *args, **kwargs: calls.append(args)
        )

        controller._on_recording_requested(QRectF(0, 0, 100, 100), "3s")
        controller._begin_armed_recording()
        first_timer = controller._countdown_timer
        _record(controller, QRectF(0, 0, 50, 50), "Off")

        # The second request must not have clobbered the first countdown,
        # nor started a recording of its own.
        assert controller._countdown_timer is first_timer
        assert backend.start_calls == []
        assert len(calls) == 1

        for _ in range(3):
            first_timer.timeout.emit()

        assert len(backend.start_calls) == 1
        assert backend.start_calls[0][0] == QRectF(0, 0, 100, 100)
        # The guard released once the countdown actually fired.
        assert controller._countdown_timer is None

    def test_a_second_commit_to_record_while_one_is_active_is_refused(
        self, make_controller, monkeypatch
    ):
        monkeypatch.setattr(
            QSystemTrayIcon, "isSystemTrayAvailable", staticmethod(lambda: True)
        )
        backend = FakeRecordingBackend()
        registry = RecorderRegistry([backend])
        controller = make_controller(
            BackendRegistry([FakeCaptureBackend(make_capture_frame())]),
            FakeTransport(make_transport_state()),
            recorder_registry=registry,
        )
        calls = []
        monkeypatch.setattr(
            controller._tray_icon, "showMessage", lambda *args, **kwargs: calls.append(args)
        )

        _record(controller, QRectF(0, 0, 100, 100), "Off")
        active = controller._active_recording
        _record(controller, QRectF(0, 0, 50, 50), "Off")

        assert len(backend.start_calls) == 1
        assert controller._active_recording is active
        assert len(calls) == 1


class TestAppControllerArmingARecording:
    """The gap between "I have chosen what to record" and "it is
    recording", which did not exist before: committing a selection started
    a backend on the spot, so the opening seconds of every recording were
    of the user getting ready.
    """

    def _controller(self, make_controller, monkeypatch, geometries=None):
        monkeypatch.setattr(
            QSystemTrayIcon, "isSystemTrayAvailable", staticmethod(lambda: True)
        )
        backend = FakeRecordingBackend()
        controller = make_controller(
            BackendRegistry([FakeCaptureBackend(make_capture_frame())]),
            FakeTransport(make_transport_state()),
            recorder_registry=RecorderRegistry([backend]),
            monitor_geometries=geometries or [QRectF(0, 0, 800, 600)],
        )
        return controller, backend

    def test_the_pill_offers_to_start_and_names_the_action(
        self, make_controller, monkeypatch
    ):
        controller, backend = self._controller(make_controller, monkeypatch)

        controller._on_recording_requested(QRectF(50, 50, 200, 150), "Off")

        assert backend.start_calls == []
        assert controller._recording_hud is not None
        assert controller._recording_hud.state() == app.RecordingHud.ARMED
        # Legible without being told: the label says what a click does.
        assert "Start" in controller._recording_hud._label.text()

    def test_clicking_the_pill_starts_an_armed_recording(
        self, make_controller, monkeypatch
    ):
        controller, backend = self._controller(make_controller, monkeypatch)
        controller._on_recording_requested(QRectF(50, 50, 200, 150), "Off")

        controller._on_hud_activated()

        assert len(backend.start_calls) == 1
        assert controller._recording_hud.state() == app.RecordingHud.RECORDING
        assert "Stop" in controller._recording_hud._label.text()

    def test_the_capture_hotkey_starts_an_armed_recording(
        self, make_controller, monkeypatch
    ):
        # The pill and the hotkey always do the same thing, so a machine
        # with nowhere to put a pill still has a way in.
        controller, backend = self._controller(make_controller, monkeypatch)
        controller._on_recording_requested(QRectF(50, 50, 200, 150), "Off")

        controller.start_capture()

        assert len(backend.start_calls) == 1
        assert controller._overlay is None

    def test_the_countdown_pill_counts_down_and_names_the_action(
        self, make_controller, monkeypatch
    ):
        controller, _backend = self._controller(make_controller, monkeypatch)
        controller._on_recording_requested(QRectF(50, 50, 200, 150), "3s")

        controller._begin_armed_recording()

        assert controller._recording_hud.state() == app.RecordingHud.COUNTING
        assert "3" in controller._recording_hud._label.text()
        assert "Cancel" in controller._recording_hud._label.text()

        controller._countdown_timer.timeout.emit()

        assert "2" in controller._recording_hud._label.text()

    def test_clicking_during_the_countdown_cancels_without_recording(
        self, make_controller, monkeypatch
    ):
        controller, backend = self._controller(make_controller, monkeypatch)
        controller._on_recording_requested(QRectF(50, 50, 200, 150), "3s")
        controller._begin_armed_recording()
        _rect, _delay, _after, path = controller._armed_recording

        controller._on_hud_activated()

        assert backend.start_calls == []
        assert controller._armed_recording is None
        assert controller._countdown_timer is None
        assert controller._recording_hud is None
        # The reserved temp file goes with it -- the next crash sweep
        # could not tell an abandoned reservation from a recording that
        # was genuinely cut short.
        assert not Path(path).exists()

    def test_the_hotkey_cancels_during_the_countdown(self, make_controller, monkeypatch):
        controller, backend = self._controller(make_controller, monkeypatch)
        controller._on_recording_requested(QRectF(50, 50, 200, 150), "3s")
        controller._begin_armed_recording()

        controller.start_capture()

        assert backend.start_calls == []
        assert controller._armed_recording is None
        assert controller._overlay is None

    def test_a_second_start_press_does_not_stack_a_second_countdown(
        self, make_controller, monkeypatch
    ):
        controller, _backend = self._controller(make_controller, monkeypatch)
        controller._on_recording_requested(QRectF(50, 50, 200, 150), "3s")
        controller._begin_armed_recording()
        first_timer = controller._countdown_timer

        controller._begin_armed_recording()

        # A second timer would strand the first, still counting towards a
        # recording nothing holds a handle to.
        assert controller._countdown_timer is first_timer

    def test_a_full_screen_recording_shows_the_pill_to_arm_then_hides_it(
        self, make_controller, monkeypatch
    ):
        controller, backend = self._controller(make_controller, monkeypatch)

        controller._on_recording_requested(None, "Off")

        # Nothing is being captured yet, so a Start control is safe here --
        # and without one there would be no way to begin a full-screen
        # recording at all.
        assert controller._recording_hud is not None
        assert controller._recording_hud.state() == app.RecordingHud.ARMED

        controller._begin_armed_recording()

        # The moment it could film itself, it goes; the tray tooltip
        # carries elapsed time from here, as it always did for this case.
        assert len(backend.start_calls) == 1
        assert controller._recording_hud is None
        assert controller._tray_icon.toolTip() == "0:00"


class TestAppControllerRecordingHud:
    """SNX-123 ticket 8: the stop control, elapsed time and tray-icon
    state that come up while a recording is running, and go back down once
    it stops.
    """

    def _start_a_recording(
        self, make_controller, monkeypatch, rect, monitor_geometries=None
    ):
        monkeypatch.setattr(
            QSystemTrayIcon, "isSystemTrayAvailable", staticmethod(lambda: True)
        )
        backend = FakeRecordingBackend()
        registry = RecorderRegistry([backend])
        controller = make_controller(
            BackendRegistry([FakeCaptureBackend(make_capture_frame())]),
            FakeTransport(make_transport_state()),
            recorder_registry=registry,
            monitor_geometries=monitor_geometries,
        )
        _record(controller, rect, "Off")
        return controller, backend

    def test_capture_hotkey_stops_an_active_recording_instead_of_starting_a_second_one(
        self, make_controller, monkeypatch
    ):
        controller, backend = self._start_a_recording(
            make_controller,
            monkeypatch,
            QRectF(50, 50, 200, 150),
            monitor_geometries=[QRectF(0, 0, 800, 600)],
        )

        # start_capture() is the single funnel every "the user asked to
        # snip" path goes through (the tray's Snip action, the hotkey, a
        # forwarded --snip request) -- it must stop the recording rather
        # than open a second overlay on top of it.
        controller.start_capture()

        assert backend.stop_calls == [True]
        assert controller._active_recording is None
        assert controller._overlay is None

    def test_a_second_hotkey_press_mid_stop_is_a_noop(self, make_controller, monkeypatch):
        controller, backend = self._start_a_recording(
            make_controller,
            monkeypatch,
            QRectF(50, 50, 200, 150),
            monitor_geometries=[QRectF(0, 0, 800, 600)],
        )
        # Simulates a second WM_HOTKEY dispatched while `backend.stop()` is
        # still on the stack (see `_stopping_recording`'s own docstring) --
        # a real Windows backend's `stop()` pumps `processEvents()` while
        # it blocks, but nothing here needs a real Windows build to
        # reproduce the same re-entrant shape.
        controller._stopping_recording = True

        controller.start_capture()

        assert backend.stop_calls == []
        assert controller._overlay is None

    def test_a_second_hud_click_mid_stop_is_a_noop(self, make_controller, monkeypatch):
        controller, backend = self._start_a_recording(
            make_controller,
            monkeypatch,
            QRectF(50, 50, 200, 150),
            monitor_geometries=[QRectF(0, 0, 800, 600)],
        )
        # The same re-entrant shape `test_a_second_hotkey_press_mid_stop_is_a_noop`
        # covers for the hotkey path, but reached through
        # `RecordingHud.mousePressEvent`'s own direct call to `_stop_recording`
        # rather than through `start_capture()` -- the guard has to hold at
        # both entry points, not just the one `start_capture()` checks.
        assert controller._recording_hud is not None
        controller._stopping_recording = True

        controller._recording_hud.mousePressEvent(None)

        assert backend.stop_calls == []

    def test_recording_can_be_stopped_with_no_tray_icon_available(
        self, make_controller, monkeypatch
    ):
        monkeypatch.setattr(
            QSystemTrayIcon, "isSystemTrayAvailable", staticmethod(lambda: False)
        )
        backend = FakeRecordingBackend()
        registry = RecorderRegistry([backend])
        controller = make_controller(
            BackendRegistry([FakeCaptureBackend(make_capture_frame())]),
            FakeTransport(make_transport_state()),
            recorder_registry=registry,
            monitor_geometries=[QRectF(0, 0, 800, 600)],
        )
        _record(controller, QRectF(50, 50, 200, 150), "Off")

        controller.start_capture()

        assert backend.stop_calls == [True]
        assert controller._active_recording is None

    def test_tray_icon_switches_to_the_recording_state_and_back(
        self, make_controller, monkeypatch
    ):
        controller, backend = self._start_a_recording(
            make_controller,
            monkeypatch,
            QRectF(50, 50, 200, 150),
            monitor_geometries=[QRectF(0, 0, 800, 600)],
        )
        idle_key = controller._idle_tray_icon.cacheKey()
        recording_key = controller._recording_tray_icon.cacheKey()
        # The two icons must actually be distinct artwork, not the same
        # pixmap reused -- otherwise the assertions below would pass
        # vacuously.
        assert idle_key != recording_key

        assert controller._tray_icon.icon().cacheKey() == recording_key

        controller._stop_recording()

        assert controller._tray_icon.icon().cacheKey() == idle_key
        assert backend.stop_calls == [True]

    def test_elapsed_time_updates_the_tray_tooltip_and_hud_label(
        self, make_controller, monkeypatch
    ):
        monkeypatch.setattr(
            QSystemTrayIcon, "isSystemTrayAvailable", staticmethod(lambda: True)
        )
        backend = FakeRecordingBackend()
        registry = RecorderRegistry([backend])
        controller = make_controller(
            BackendRegistry([FakeCaptureBackend(make_capture_frame())]),
            FakeTransport(make_transport_state()),
            recorder_registry=registry,
            monitor_geometries=[QRectF(0, 0, 1000, 800)],
        )
        # One `time.monotonic()` call stamps `_recording_started_at`, a
        # second is the tick handler's own immediate call (so a recording
        # stopped inside the first second still showed 0:00 rather than
        # nothing), and a third is the explicit tick below -- 65 real
        # seconds later.
        clock = iter([100.0, 100.0, 165.0])
        monkeypatch.setattr(app.time, "monotonic", lambda: next(clock))

        _record(controller, QRectF(50, 50, 200, 150), "Off")

        assert controller._tray_icon.toolTip() == "0:00"
        assert controller._recording_hud is not None
        # The pill names the action, not just the time -- clicking it
        # stops the recording, and nothing used to say so.
        assert controller._recording_hud._label.text() == "Stop  ·  0:00"
        assert controller._recording_hud.state() == app.RecordingHud.RECORDING

        controller._on_recording_tick()

        assert controller._tray_icon.toolTip() == "1:05"
        assert controller._recording_hud._label.text() == "Stop  ·  1:05"

    def test_no_hud_for_a_full_screen_recording(self, make_controller, monkeypatch):
        controller, _backend = self._start_a_recording(
            make_controller,
            monkeypatch,
            None,
            monitor_geometries=[QRectF(0, 0, 1000, 800)],
        )

        assert controller._recording_hud is None

    def test_no_hud_when_there_is_no_room_for_one(self, make_controller, monkeypatch):
        # A tiny desktop with the recorded rect filling it entirely: no
        # side has room for a 150x44 pill plus its margin.
        controller, _backend = self._start_a_recording(
            make_controller,
            monkeypatch,
            QRectF(0, 0, 100, 100),
            monitor_geometries=[QRectF(0, 0, 100, 100)],
        )

        assert controller._recording_hud is None

    def test_elapsed_time_falls_back_to_stdout_with_no_tray_and_no_hud_room(
        self, make_controller, monkeypatch, capsys
    ):
        # Neither surface is available: no tray to hang a tooltip nobody
        # would see anyway, and (the same tiny, fully-filled desktop
        # `test_no_hud_when_there_is_no_room_for_one` uses) no room for the
        # HUD either. Elapsed time must still show up somewhere rather than
        # nowhere at all -- stdout is the same last-resort `_report_shortcut`
        # already uses for "no tray to hang a message on" (SNX-54).
        monkeypatch.setattr(
            QSystemTrayIcon, "isSystemTrayAvailable", staticmethod(lambda: False)
        )
        backend = FakeRecordingBackend()
        registry = RecorderRegistry([backend])
        controller = make_controller(
            BackendRegistry([FakeCaptureBackend(make_capture_frame())]),
            FakeTransport(make_transport_state()),
            recorder_registry=registry,
            monitor_geometries=[QRectF(0, 0, 100, 100)],
        )
        clock = iter([100.0, 100.0])
        monkeypatch.setattr(app.time, "monotonic", lambda: next(clock))

        _record(controller, QRectF(0, 0, 100, 100), "Off")

        assert controller._recording_hud is None
        assert "0:00" in capsys.readouterr().out

    def test_hud_is_placed_outside_the_recorded_rect(self, make_controller, monkeypatch):
        rect = QRectF(50, 50, 200, 150)
        controller, _backend = self._start_a_recording(
            make_controller,
            monkeypatch,
            rect,
            monitor_geometries=[QRectF(0, 0, 1000, 800)],
        )

        assert controller._recording_hud is not None
        hud_rect = QRectF(controller._recording_hud.geometry())
        assert not hud_rect.intersects(rect)

    def test_stop_recording_closes_the_hud_and_stops_the_elapsed_timer(
        self, make_controller, monkeypatch
    ):
        controller, backend = self._start_a_recording(
            make_controller,
            monkeypatch,
            QRectF(50, 50, 200, 150),
            monitor_geometries=[QRectF(0, 0, 1000, 800)],
        )
        hud = controller._recording_hud
        timer = controller._recording_elapsed_timer
        assert hud is not None
        assert timer is not None

        controller._stop_recording()

        assert controller._recording_hud is None
        assert controller._recording_elapsed_timer is None
        assert not hud.isVisible()
        assert not timer.isActive()
        assert controller._tray_icon.toolTip() == ""
        assert backend.stop_calls == [True]

    def test_stopping_recording_reports_a_backend_failure_without_raising(
        self, make_controller, monkeypatch
    ):
        monkeypatch.setattr(
            QSystemTrayIcon, "isSystemTrayAvailable", staticmethod(lambda: True)
        )
        backend = FakeRecordingBackend(stop_error=RuntimeError("boom"))
        registry = RecorderRegistry([backend])
        controller = make_controller(
            BackendRegistry([FakeCaptureBackend(make_capture_frame())]),
            FakeTransport(make_transport_state()),
            recorder_registry=registry,
            monitor_geometries=[QRectF(0, 0, 800, 600)],
        )
        _record(controller, QRectF(50, 50, 200, 150), "Off")
        calls = []
        monkeypatch.setattr(
            controller._tray_icon, "showMessage", lambda *args, **kwargs: calls.append(args)
        )

        controller._stop_recording()

        assert len(calls) == 1
        assert "boom" in calls[0][1]
        # Cleared even though the backend call failed -- the visible state
        # and the backend's actual state are reported independently.
        assert controller._active_recording is None
        assert controller._stopping_recording is False

    def test_stopping_with_nothing_recording_is_a_noop(self, make_controller):
        controller = make_controller(
            BackendRegistry([FakeCaptureBackend(make_capture_frame())]),
            FakeTransport(make_transport_state()),
            monitor_geometries=[QRectF(0, 0, 800, 600)],
        )

        controller._stop_recording()  # must not raise

        assert controller._active_recording is None


class TestAppControllerRecorderUnavailable:
    """A platform with nothing behind `build_recording_registry()` yet
    (macOS today) must not stop `AppController` from constructing -- the
    same "a failure must not stop the rest" rule
    `run_first_launch_setup`'s own `UnimplementedPlatformError` handling
    already follows for desktop integration, applied here (SNX-122).
    """

    def _raise_unimplemented(self):
        raise app.platform.UnimplementedPlatformError("macOS", "build_recording_registry")

    def test_construction_survives_an_unimplemented_recording_seam(self, monkeypatch):
        monkeypatch.setattr(
            app.platform.current, "build_recording_registry", self._raise_unimplemented
        )

        controller = AppController(
            BackendRegistry([FakeCaptureBackend(make_capture_frame())]),
            FakeTransport(make_transport_state()),
        )
        try:
            assert controller._recorder_registry is None
        finally:
            controller._tray_icon.hide()

    def test_a_later_commit_to_record_reports_the_remembered_message(self, monkeypatch):
        monkeypatch.setattr(
            app.platform.current, "build_recording_registry", self._raise_unimplemented
        )
        monkeypatch.setattr(
            QSystemTrayIcon, "isSystemTrayAvailable", staticmethod(lambda: True)
        )
        controller = AppController(
            BackendRegistry([FakeCaptureBackend(make_capture_frame())]),
            FakeTransport(make_transport_state()),
        )
        calls = []
        monkeypatch.setattr(
            controller._tray_icon, "showMessage", lambda *args, **kwargs: calls.append(args)
        )
        try:
            _record(controller, QRectF(0, 0, 100, 100), "Off")

            assert len(calls) == 1
            assert "macOS" in calls[0][1]
            assert "build_recording_registry" in calls[0][1]
        finally:
            controller._tray_icon.hide()


class TestAppControllerLandingRecording:
    """SNX-124 (recording.md ticket 9): what happens to the temp file once
    a recording actually stops -- move it into the configured save
    folder/filename convention, act on `after`, and toast where it went.
    `_recordings_land_in_a_temp_folder` (this file's own autouse fixture)
    already points `load_save_folder()` at `tmp_path`; tests here read
    `tmp_path` directly to check what actually landed there.
    """

    def _start_a_recording(self, make_controller, monkeypatch, after="instant"):
        monkeypatch.setattr(
            QSystemTrayIcon, "isSystemTrayAvailable", staticmethod(lambda: True)
        )
        backend = FakeRecordingBackend()
        registry = RecorderRegistry([backend])
        controller = make_controller(
            BackendRegistry([FakeCaptureBackend(make_capture_frame())]),
            FakeTransport(make_transport_state()),
            recorder_registry=registry,
            monitor_geometries=[QRectF(0, 0, 800, 600)],
        )
        _record(controller, QRectF(0, 0, 100, 100), "Off", after)
        return controller, backend

    def test_stopping_moves_the_temp_file_into_the_save_folder(
        self, make_controller, monkeypatch, tmp_path
    ):
        controller, backend = self._start_a_recording(make_controller, monkeypatch)
        _rect, temp_path = backend.start_calls[0]

        controller._stop_recording()

        assert not Path(temp_path).exists()
        landed = list(tmp_path.iterdir())
        assert len(landed) == 1
        assert landed[0].suffix == ".mp4"

    def test_the_landed_filename_matches_preview_filenames_own_computation(
        self, make_controller, monkeypatch, tmp_path
    ):
        # AC: the real filename is the same computation Settings' own
        # preview label renders, not a second, independent guess at it.
        controller, backend = self._start_a_recording(make_controller, monkeypatch)

        controller._stop_recording()

        landed = next(tmp_path.iterdir())
        expected = setup_desktop.preview_filename(
            tmp_path, setup_desktop.load_filename_pattern(), extension="mp4"
        )
        assert landed == Path(expected)

    def test_instant_copies_the_landed_path_not_the_temp_path(
        self, make_controller, monkeypatch, tmp_path
    ):
        controller, backend = self._start_a_recording(
            make_controller, monkeypatch, after="instant"
        )
        copied = []
        monkeypatch.setattr(app, "copy_file_to_clipboard", lambda path: copied.append(path))

        controller._stop_recording()

        assert len(copied) == 1
        assert copied[0].parent == tmp_path
        assert copied[0].exists()

    def test_save_performs_no_clipboard_action(self, make_controller, monkeypatch, tmp_path):
        controller, backend = self._start_a_recording(make_controller, monkeypatch, after="save")
        copied = []
        monkeypatch.setattr(app, "copy_file_to_clipboard", lambda path: copied.append(path))

        controller._stop_recording()

        assert copied == []
        assert len(list(tmp_path.iterdir())) == 1

    def test_a_normal_stop_reports_the_landed_path_through_the_tray(
        self, make_controller, monkeypatch, tmp_path
    ):
        controller, backend = self._start_a_recording(make_controller, monkeypatch)
        calls = []
        monkeypatch.setattr(
            controller._tray_icon, "showMessage", lambda *args, **kwargs: calls.append(args)
        )

        controller._stop_recording()

        assert len(calls) == 1
        landed = next(tmp_path.iterdir())
        assert str(landed) in calls[0][1]

    def test_a_failed_move_is_reported_not_raised(self, make_controller, monkeypatch):
        # Review finding: `shutil.move` into the save folder is a real
        # cross-filesystem copy whenever the two don't share a mount, which
        # needs free space precisely in the disk-exhaustion scenario ticket
        # 9 is about -- an `OSError` out of it must become a toast, not an
        # exception escaping a Qt slot (the HUD stop button, the hotkey
        # handler, or the disk-space tick itself).
        controller, backend = self._start_a_recording(make_controller, monkeypatch)
        monkeypatch.setattr(
            app.shutil, "move", lambda *a, **k: (_ for _ in ()).throw(OSError("No space left on device"))
        )
        calls = []
        monkeypatch.setattr(
            controller._tray_icon, "showMessage", lambda *args, **kwargs: calls.append(args)
        )

        controller._stop_recording()  # must not raise

        assert len(calls) == 1
        assert "failed" in calls[0][1].lower()
        assert controller._active_recording is None
        assert controller._stopping_recording is False


class TestAppControllerDiscardRecording:
    """SNX-124 (recording.md ticket 9): the tray's own 'Discard recording'
    action -- stop the backend and throw the temp file away, no move, no
    clipboard, a distinct tray message.
    """

    def _start_a_recording(self, make_controller, monkeypatch):
        monkeypatch.setattr(
            QSystemTrayIcon, "isSystemTrayAvailable", staticmethod(lambda: True)
        )
        backend = FakeRecordingBackend()
        registry = RecorderRegistry([backend])
        controller = make_controller(
            BackendRegistry([FakeCaptureBackend(make_capture_frame())]),
            FakeTransport(make_transport_state()),
            recorder_registry=registry,
            monitor_geometries=[QRectF(0, 0, 800, 600)],
        )
        _record(controller, QRectF(0, 0, 100, 100), "Off")
        return controller, backend

    def test_discard_action_is_enabled_while_recording_and_disabled_after(
        self, make_controller, monkeypatch
    ):
        controller, _backend = self._start_a_recording(make_controller, monkeypatch)

        assert controller.discard_action.isEnabled() is True

        controller._stop_recording()

        assert controller.discard_action.isEnabled() is False

    def test_discard_stops_the_backend_and_deletes_the_temp_file(
        self, make_controller, monkeypatch, tmp_path
    ):
        controller, backend = self._start_a_recording(make_controller, monkeypatch)
        _rect, temp_path = backend.start_calls[0]

        controller._discard_recording()

        assert backend.stop_calls == [True]
        assert not Path(temp_path).exists()
        # No move: the save folder this file's autouse fixture points at
        # must stay empty.
        assert list(tmp_path.iterdir()) == []

    def test_discard_performs_no_clipboard_action(self, make_controller, monkeypatch):
        controller, backend = self._start_a_recording(make_controller, monkeypatch)
        copied = []
        monkeypatch.setattr(app, "copy_file_to_clipboard", lambda path: copied.append(path))

        controller._discard_recording()

        assert copied == []

    def test_discard_reports_a_distinct_message_from_a_normal_stop(
        self, make_controller, monkeypatch
    ):
        controller, backend = self._start_a_recording(make_controller, monkeypatch)
        calls = []
        monkeypatch.setattr(
            controller._tray_icon, "showMessage", lambda *args, **kwargs: calls.append(args)
        )

        controller._discard_recording()

        assert len(calls) == 1
        assert "discard" in calls[0][1].lower()

    def test_discarding_with_nothing_recording_is_a_noop(self, make_controller):
        controller = make_controller(
            BackendRegistry([FakeCaptureBackend(make_capture_frame())]),
            FakeTransport(make_transport_state()),
            monitor_geometries=[QRectF(0, 0, 800, 600)],
        )

        controller._discard_recording()  # must not raise

        assert controller._active_recording is None


class TestAppControllerRecordingTempFileCleanup:
    """SNX-124 (recording.md ticket 9): a temp file left by a previous
    crashed run must not accumulate -- `AppController.__init__` sweeps
    `app._recording_temp_dir()` before anything else runs.
    """

    def test_a_leftover_file_is_deleted_on_construction(self, make_controller):
        leftover = app._recording_temp_dir() / "crashed-recording.mp4"
        leftover.write_bytes(b"leftover")

        make_controller(
            BackendRegistry([FakeCaptureBackend(make_capture_frame())]),
            FakeTransport(make_transport_state()),
            monitor_geometries=[QRectF(0, 0, 800, 600)],
        )

        assert not leftover.exists()

    def test_recording_temp_files_are_created_under_the_dedicated_subdirectory(
        self, make_controller, monkeypatch
    ):
        monkeypatch.setattr(
            QSystemTrayIcon, "isSystemTrayAvailable", staticmethod(lambda: True)
        )
        backend = FakeRecordingBackend()
        registry = RecorderRegistry([backend])
        controller = make_controller(
            BackendRegistry([FakeCaptureBackend(make_capture_frame())]),
            FakeTransport(make_transport_state()),
            recorder_registry=registry,
        )

        _record(controller, QRectF(0, 0, 100, 100), "Off")

        _rect, path = backend.start_calls[0]
        assert Path(path).parent == app._recording_temp_dir()


class TestAppControllerRecordingDiskSpace:
    """SNX-124 (recording.md ticket 9): running out of disk during a
    recording is reported, not failed silently -- piggybacked on the
    once-a-second elapsed-time tick via an injected `disk_usage` factory.
    """

    def _disk_usage_reporting(self, free_bytes):
        return lambda path: SimpleNamespace(free=free_bytes)

    def test_low_free_space_stops_the_recording_within_one_tick(
        self, make_controller, monkeypatch, tmp_path
    ):
        monkeypatch.setattr(
            QSystemTrayIcon, "isSystemTrayAvailable", staticmethod(lambda: True)
        )
        backend = FakeRecordingBackend()
        registry = RecorderRegistry([backend])
        controller = make_controller(
            BackendRegistry([FakeCaptureBackend(make_capture_frame())]),
            FakeTransport(make_transport_state()),
            recorder_registry=registry,
            monitor_geometries=[QRectF(0, 0, 800, 600)],
            disk_usage=self._disk_usage_reporting(1024),
        )
        calls = []
        monkeypatch.setattr(
            controller._tray_icon, "showMessage", lambda *args, **kwargs: calls.append(args)
        )

        # `_start_recording_ui` fires the elapsed-time tick once immediately
        # (SNX-123 ticket 8, so a recording stopped inside the first second
        # still shows 0:00 somewhere) -- the low-disk check piggybacks on
        # that same tick, so low free space is caught on this very first
        # one, with no separate explicit tick needed.
        _record(controller, QRectF(0, 0, 100, 100), "Off")

        assert backend.stop_calls == [True]
        assert controller._active_recording is None
        assert any("disk" in call[1].lower() for call in calls)

    def test_low_disk_stop_shows_one_toast_naming_where_it_landed(
        self, make_controller, monkeypatch, tmp_path
    ):
        # Review finding: `_check_recording_disk_space()` used to call
        # `_stop_recording()` (which itself toasts "Recording saved to
        # ..."), then show its own, separate low-disk message right after
        # -- two tray notifications for one event, with the first liable to
        # be clipped before it's read. There must be exactly one toast, and
        # it must still say where the file went.
        monkeypatch.setattr(
            QSystemTrayIcon, "isSystemTrayAvailable", staticmethod(lambda: True)
        )
        backend = FakeRecordingBackend()
        registry = RecorderRegistry([backend])
        controller = make_controller(
            BackendRegistry([FakeCaptureBackend(make_capture_frame())]),
            FakeTransport(make_transport_state()),
            recorder_registry=registry,
            monitor_geometries=[QRectF(0, 0, 800, 600)],
            disk_usage=self._disk_usage_reporting(1024),
        )
        calls = []
        monkeypatch.setattr(
            controller._tray_icon, "showMessage", lambda *args, **kwargs: calls.append(args)
        )

        _record(controller, QRectF(0, 0, 100, 100), "Off")

        assert len(calls) == 1
        landed = next(tmp_path.iterdir())
        assert str(landed) in calls[0][1]
        assert "disk" in calls[0][1].lower()

    def test_watches_the_temp_files_own_filesystem_not_the_save_folder(
        self, make_controller, monkeypatch, tmp_path
    ):
        # Review finding: the file actually growing during a recording
        # lives under `_recording_temp_dir()` (system temp), not the save
        # folder `_land_recording` only moves into once the recording is
        # done -- a tmpfs-mounted temp dir can fill up long before the save
        # folder's own free space would ever look low, so the check must
        # stat the former, not the latter.
        monkeypatch.setattr(
            QSystemTrayIcon, "isSystemTrayAvailable", staticmethod(lambda: True)
        )
        backend = FakeRecordingBackend()
        registry = RecorderRegistry([backend])
        queried = []

        def _disk_usage(path):
            queried.append(path)
            return SimpleNamespace(free=10 * 1024 * 1024 * 1024)

        controller = make_controller(
            BackendRegistry([FakeCaptureBackend(make_capture_frame())]),
            FakeTransport(make_transport_state()),
            recorder_registry=registry,
            monitor_geometries=[QRectF(0, 0, 800, 600)],
            disk_usage=_disk_usage,
        )

        _record(controller, QRectF(0, 0, 100, 100), "Off")

        _rect, path = backend.start_calls[0]
        assert queried == [str(Path(path).parent)]
        assert queried[0] != str(tmp_path)

    def test_plenty_of_free_space_does_not_touch_the_recording(
        self, make_controller, monkeypatch
    ):
        monkeypatch.setattr(
            QSystemTrayIcon, "isSystemTrayAvailable", staticmethod(lambda: True)
        )
        backend = FakeRecordingBackend()
        registry = RecorderRegistry([backend])
        controller = make_controller(
            BackendRegistry([FakeCaptureBackend(make_capture_frame())]),
            FakeTransport(make_transport_state()),
            recorder_registry=registry,
            monitor_geometries=[QRectF(0, 0, 800, 600)],
            disk_usage=self._disk_usage_reporting(10 * 1024 * 1024 * 1024),
        )
        _record(controller, QRectF(0, 0, 100, 100), "Off")

        controller._on_recording_tick()

        assert backend.stop_calls == []
        assert controller._active_recording is not None


class TestAppControllerOverlayDismissal:
    """SNX-62: `OverlayWindow.copy()`/`save()` used to flatten the image,
    toast, and return without ever dismissing the overlay -- so
    `start_capture()`'s re-entrancy guard (which read `self._overlay.
    isVisible()`) refused every later Snip request for the rest of the
    session, the instant a user pressed Copy or Save once. Drives the real
    floating-bar buttons through a real, controller-opened overlay -- not
    `overlay.copy()`/`overlay.save()` called directly, since the bug was
    specifically in what clicking the button did (or didn't) to the window,
    not in `copy()`/`save()`'s own flatten-and-emit behaviour, which
    test_overlay.py already covers.
    """

    def _start_capture_with_selection(self, make_controller):
        registry = BackendRegistry([FakeCaptureBackend(make_capture_frame())])
        controller = make_controller(
            registry,
            FakeTransport(make_transport_state()),
            monitor_geometries=[QRectF(0, 0, 200, 200)],
        )
        controller.start_capture()
        overlay = controller._overlay
        QTest.qWaitForWindowExposed(overlay)
        overlay.set_selection(QRect(0, 0, 200, 200))
        return controller, overlay

    def test_copy_dismisses_the_overlay_and_a_later_snip_is_not_refused(
        self, make_controller, monkeypatch
    ):
        monkeypatch.setattr(app, "copy_image_to_clipboard", lambda image: None)
        controller, first_overlay = self._start_capture_with_selection(make_controller)

        QTest.mouseClick(first_overlay._bar._copy_button, Qt.MouseButton.LeftButton)

        # The bug: this used to still be `first_overlay`, still isVisible(),
        # forever -- so the request below was silently swallowed.
        assert controller._overlay is None

        controller.start_capture()

        assert controller._overlay is not None
        assert controller._overlay is not first_overlay

    def test_save_dismisses_the_overlay_and_a_later_snip_is_not_refused(
        self, make_controller, monkeypatch, tmp_path
    ):
        monkeypatch.setattr(app.Path, "home", lambda: tmp_path)
        controller, first_overlay = self._start_capture_with_selection(make_controller)

        QTest.mouseClick(first_overlay._bar._save_button, Qt.MouseButton.LeftButton)

        assert controller._overlay is None

        controller.start_capture()

        assert controller._overlay is not None
        assert controller._overlay is not first_overlay


class TestAppControllerTrayMenu:
    def test_tray_menu_offers_snip_settings_and_quit(self, make_controller):
        controller = make_controller(
            BackendRegistry(), FakeTransport(make_transport_state()), monitor_geometries=[]
        )

        # The old per-SelectionMode items are gone: OverlayWindow's own
        # capture-mode popover is what picks Region/Window/Full screen/
        # Freeform once the overlay is open, per the ticket.
        assert controller.snip_action.text() == "Snip"
        assert controller.quit_action.text() == "Quit"
        # Settings sits between them: it is the second thing anyone opens a
        # tray menu for, and it must not be below Quit. Discard recording
        # (recording.md ticket 9) sits after it, disabled until a
        # recording is actually active -- see TestAppControllerDiscard.
        assert [action.text() for action in controller._tray_icon.contextMenu().actions()] == [
            "Snip",
            "Settings...",
            "Discard recording",
            "Quit",
        ]

    def test_discard_action_starts_disabled(self, make_controller):
        controller = make_controller(
            BackendRegistry(), FakeTransport(make_transport_state()), monitor_geometries=[]
        )

        assert controller.discard_action.isEnabled() is False

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


class TestAppControllerIcon:
    """SNX-81: the tray icon and the application's window icon are both
    the vendored `design/logo/` artwork now, not a drawn placeholder.
    """

    def test_tray_icon_is_the_vendored_artwork_not_a_drawn_placeholder(
        self, make_controller
    ):
        controller = make_controller(
            BackendRegistry(), FakeTransport(make_transport_state()), monitor_geometries=[]
        )

        # The old placeholder was a single 32x32 solid-colour QPixmap --
        # more than one available size only happens once real multi-size
        # artwork has been loaded.
        assert len(controller._tray_icon.icon().availableSizes()) > 1

    def test_sets_the_applications_window_icon_from_the_same_artwork(
        self, make_controller
    ):
        controller = make_controller(
            BackendRegistry(), FakeTransport(make_transport_state()), monitor_geometries=[]
        )

        window_icon = QApplication.instance().windowIcon()
        assert not window_icon.isNull()
        assert window_icon.cacheKey() == controller._tray_icon.icon().cacheKey()

    def test_tray_still_starts_when_the_artwork_cannot_be_loaded(
        self, make_controller, monkeypatch, tmp_path
    ):
        # AC: a broken/missing icon must not stop the tray -- and the whole
        # resident process -- from starting.
        monkeypatch.setattr(app, "_LOGO_DIR", tmp_path / "no-such-dir")

        controller = make_controller(
            BackendRegistry(), FakeTransport(make_transport_state()), monitor_geometries=[]
        )

        assert not controller._tray_icon.icon().isNull()
        assert controller.snip_action.text() == "Snip"


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

    def test_running_resident_for_the_first_time_runs_first_launch_setup(self, monkeypatch):
        # AC: "the first launch installs the desktop integration and binds
        # the shortcut without the user running --setup" -- proving
        # _become_resident() actually reaches run_first_launch_setup(), the
        # same way it already proves install_hotkey_listener() is reached
        # for the process that really becomes resident.
        monkeypatch.setattr(QApplication, "exec", lambda self: 0)

        calls = []
        created = []

        class TrackingAppController(AppController):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                created.append(self)

            def run_first_launch_setup(self):
                calls.append(True)

        monkeypatch.setattr(app, "AppController", TrackingAppController)

        state = make_transport_state()
        transport = FakeTransport(state)

        try:
            result = run_resident_app(registry=BackendRegistry(), transport=transport)

            assert result == 0
            assert calls == [True]
        finally:
            created[0]._tray_icon.hide()

    def test_running_resident_calls_ensure_stable_install(self, monkeypatch):
        # SNX-103: proves _become_resident() reaches
        # platform.current.ensure_stable_install() -- unlike
        # run_first_launch_setup() just above, this has to run on *every*
        # launch that becomes resident, not just the first, so a newer
        # portable download run over an already-set-up older install
        # still relocates itself rather than the record above staying
        # silent forever after the first launch.
        monkeypatch.setattr(QApplication, "exec", lambda self: 0)

        calls = []
        monkeypatch.setattr(
            app.platform.current, "ensure_stable_install", lambda: calls.append(True) or None
        )

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
            assert calls == [True]
        finally:
            created[0]._tray_icon.hide()


class TestShortcutFlag:
    """`--shortcut` modifies `--setup` rather than being an action itself."""

    def test_is_passed_through_to_install_desktop_integration(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(
            app.platform.current,
            "install_desktop_integration",
            lambda **kwargs: seen.update(kwargs) or 0,
        )

        assert main(["--setup", "--shortcut", "<Super><Shift>x"]) == 0
        assert seen["shortcut"] == "<Super><Shift>x"

    def test_setup_alone_passes_none_so_the_stored_value_wins(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(
            app.platform.current,
            "install_desktop_integration",
            lambda **kwargs: seen.update(kwargs) or 0,
        )

        main(["--setup"])

        assert seen["shortcut"] is None

    def test_is_rejected_without_setup(self, capsys):
        with pytest.raises(SystemExit):
            main(["--shortcut", "<Super><Shift>x"])

        assert "only means anything alongside --setup" in capsys.readouterr().err


class TestWindowsHotkeyIntegration:
    """SNX-91: the resident app registers a global hotkey on Windows and
    starts a capture when it fires, even while another application has
    focus, and Settings' Save button re-registers it without a restart.

    Exercised through `AppController.install_hotkey_listener()` (what
    `app.py`'s `_become_resident()` calls once the process is actually
    resident, not `AppController.__init__` -- see that method's own
    docstring) with `platform.current`'s real `RegisterHotKey` calls
    mocked out, the same way `TestWindowsPlatform` (test_platform.py) keeps
    the suite from grabbing a real system-wide hotkey on whatever machine
    runs it. `HotkeyEventFilter.is_available()` is forced rather than relied
    on, so this coverage holds regardless of which OS actually runs pytest.
    """

    def test_is_a_no_op_when_unavailable(self, make_controller, monkeypatch):
        monkeypatch.setattr(app.HotkeyEventFilter, "is_available", staticmethod(lambda: False))
        install_calls = []
        monkeypatch.setattr(
            QApplication, "installNativeEventFilter", lambda self, f: install_calls.append(f)
        )
        controller = make_controller(BackendRegistry(), FakeTransport(make_transport_state()))

        controller.install_hotkey_listener()

        assert controller.hotkey_filter is None
        assert install_calls == []

    def test_installs_a_filter_and_registers_the_default_shortcut(
        self, make_controller, monkeypatch
    ):
        monkeypatch.setattr(app.HotkeyEventFilter, "is_available", staticmethod(lambda: True))
        install_calls = []
        monkeypatch.setattr(
            QApplication, "installNativeEventFilter", lambda self, f: install_calls.append(f)
        )
        bind_calls = []
        monkeypatch.setattr(
            app.platform.current,
            "bind_shortcut",
            lambda shortcut=None: bind_calls.append(shortcut)
            or "Bound Control+Alt+S to start a snip.",
        )
        monkeypatch.setattr(
            app.platform.current, "registered_shortcut", "Control+Alt+S", raising=False
        )
        controller = make_controller(BackendRegistry(), FakeTransport(make_transport_state()))

        controller.install_hotkey_listener()

        assert isinstance(controller.hotkey_filter, app.HotkeyEventFilter)
        assert install_calls == [controller.hotkey_filter]
        # No shortcut passed explicitly -- bind_shortcut()'s own default
        # (fall back to setup_desktop.load_shortcut(), Control+Alt+S the
        # first time) must reach it untouched, the same contract
        # LinuxPlatform.bind_shortcut() already has.
        assert bind_calls == [None]

    def test_a_clash_at_startup_is_reported_through_the_tray_by_name(
        self, make_controller, monkeypatch
    ):
        monkeypatch.setattr(app.HotkeyEventFilter, "is_available", staticmethod(lambda: True))
        monkeypatch.setattr(QApplication, "installNativeEventFilter", lambda self, f: None)
        clash_message = (
            "Control+Alt+S is already in use by another application -- "
            "snipux cannot use it too."
        )
        monkeypatch.setattr(
            app.platform.current, "bind_shortcut", lambda shortcut=None: clash_message
        )
        monkeypatch.setattr(app.platform.current, "registered_shortcut", None, raising=False)
        monkeypatch.setattr(
            QSystemTrayIcon, "isSystemTrayAvailable", staticmethod(lambda: True)
        )
        calls = []
        monkeypatch.setattr(
            QSystemTrayIcon,
            "showMessage",
            lambda self, title, message, *a, **k: calls.append(message),
        )
        controller = make_controller(BackendRegistry(), FakeTransport(make_transport_state()))

        controller.install_hotkey_listener()

        assert calls == [clash_message]

    def test_a_successful_bind_at_startup_is_not_reported(
        self, make_controller, monkeypatch
    ):
        # A clash is the failure this AC is about; a routine successful
        # bind at every startup is not worth a balloon every time -- see
        # TestSnipFlag's own tray-message assertions, which this would
        # otherwise silently inflate.
        monkeypatch.setattr(app.HotkeyEventFilter, "is_available", staticmethod(lambda: True))
        monkeypatch.setattr(QApplication, "installNativeEventFilter", lambda self, f: None)
        monkeypatch.setattr(
            app.platform.current,
            "bind_shortcut",
            lambda shortcut=None: "Bound Control+Alt+S to start a snip.",
        )
        monkeypatch.setattr(
            app.platform.current, "registered_shortcut", "Control+Alt+S", raising=False
        )
        monkeypatch.setattr(
            QSystemTrayIcon, "isSystemTrayAvailable", staticmethod(lambda: True)
        )
        calls = []
        monkeypatch.setattr(
            QSystemTrayIcon,
            "showMessage",
            lambda self, title, message, *a, **k: calls.append(message),
        )
        controller = make_controller(BackendRegistry(), FakeTransport(make_transport_state()))

        controller.install_hotkey_listener()

        assert calls == []

    def test_pressing_the_hotkey_starts_a_capture(self, make_controller, monkeypatch):
        # AC: the hotkey works while another application has focus -- there
        # is no window/focus concept anywhere in reaching start_capture()
        # this way, which is the whole point of a WM_HOTKEY thread message
        # rather than a window event.
        monkeypatch.setattr(app.HotkeyEventFilter, "is_available", staticmethod(lambda: True))
        monkeypatch.setattr(QApplication, "installNativeEventFilter", lambda self, f: None)
        monkeypatch.setattr(
            app.platform.current,
            "bind_shortcut",
            lambda shortcut=None: "Bound Control+Alt+S to start a snip.",
        )
        monkeypatch.setattr(
            app.platform.current, "registered_shortcut", "Control+Alt+S", raising=False
        )
        registry = BackendRegistry([FakeCaptureBackend(make_capture_frame())])
        controller = make_controller(
            registry,
            FakeTransport(make_transport_state()),
            monitor_geometries=[QRectF(0, 0, 200, 200)],
        )
        controller.install_hotkey_listener()
        msg = windows_platform._MSG(message=windows_platform._WM_HOTKEY)

        controller.hotkey_filter.nativeEventFilter(b"windows_generic_MSG", ctypes.addressof(msg))

        assert isinstance(controller._overlay, OverlayWindow)

    def test_settings_saved_rebinds_through_the_platform_seam_on_windows(
        self, make_controller, monkeypatch
    ):
        # AC: changing the shortcut re-registers it without restarting the
        # app -- via platform.current.bind_shortcut(), not the Linux-only
        # setup_desktop.bind_gnome_shortcut() path _on_settings_saved()
        # otherwise takes.
        monkeypatch.setattr(app.HotkeyEventFilter, "is_available", staticmethod(lambda: True))
        bind_calls = []
        monkeypatch.setattr(
            app.platform.current,
            "bind_shortcut",
            lambda shortcut=None: bind_calls.append(shortcut)
            or "Bound Control+Alt+X to start a snip.",
        )
        gnome_calls = []
        monkeypatch.setattr(
            app.setup_desktop,
            "bind_gnome_shortcut",
            lambda *a, **k: gnome_calls.append(a) or "unused",
        )
        controller = make_controller(BackendRegistry(), FakeTransport(make_transport_state()))

        controller._on_settings_saved()

        assert bind_calls == [None]
        assert gnome_calls == []


class TestRunFirstLaunchSetup:
    """SNX-95: `AppController.run_first_launch_setup()` is what lets a bare
    `snipux` launch install desktop integration and bind the shortcut
    without anyone having to know to run `--setup` first. Exercised
    directly on a constructed controller here -- `_become_resident()`'s own
    call to it is covered separately, in TestRunResidentApp, the same split
    `install_hotkey_listener()` already has between this file's
    TestWindowsHotkeyIntegration and TestRunResidentApp.
    """

    def test_skips_and_touches_nothing_once_already_recorded_complete(
        self, make_controller, monkeypatch
    ):
        monkeypatch.setattr(app.setup_desktop, "load_setup_complete", lambda cd=None: True)

        def install_must_not_be_called(**kwargs):
            raise AssertionError(
                "install_desktop_integration() must not run once already recorded complete"
            )

        monkeypatch.setattr(
            app.platform.current, "install_desktop_integration", install_must_not_be_called
        )

        def save_must_not_be_called(enabled, cd=None):
            raise AssertionError(
                "save_setup_complete() must not rewrite an already-complete record"
            )

        monkeypatch.setattr(app.setup_desktop, "save_setup_complete", save_must_not_be_called)
        controller = make_controller(BackendRegistry(), FakeTransport(make_transport_state()))
        report_calls = []
        monkeypatch.setattr(controller, "_report_shortcut", lambda message: report_calls.append(message))

        controller.run_first_launch_setup()  # must not raise

        assert report_calls == []

    def test_installs_desktop_integration_and_records_completion_on_first_run(
        self, make_controller, monkeypatch
    ):
        monkeypatch.setattr(app.setup_desktop, "load_setup_complete", lambda cd=None: False)
        install_calls = []
        monkeypatch.setattr(
            app.platform.current,
            "install_desktop_integration",
            lambda **kwargs: install_calls.append(kwargs) or 0,
        )
        save_calls = []
        monkeypatch.setattr(
            app.setup_desktop,
            "save_setup_complete",
            lambda enabled, cd=None: save_calls.append(enabled) or True,
        )
        controller = make_controller(BackendRegistry(), FakeTransport(make_transport_state()))

        controller.run_first_launch_setup()

        assert install_calls == [{}]
        assert save_calls == [True]

    def test_reports_what_it_set_up_and_how_to_change_it(self, make_controller, monkeypatch):
        monkeypatch.setattr(app.setup_desktop, "load_setup_complete", lambda cd=None: False)
        monkeypatch.setattr(app.platform.current, "install_desktop_integration", lambda **kwargs: 0)
        monkeypatch.setattr(app.setup_desktop, "save_setup_complete", lambda enabled, cd=None: True)
        monkeypatch.setattr(app.setup_desktop, "load_shortcut", lambda cd=None: "Control+Alt+S")
        monkeypatch.setattr(QSystemTrayIcon, "isSystemTrayAvailable", staticmethod(lambda: True))
        calls = []
        monkeypatch.setattr(
            QSystemTrayIcon,
            "showMessage",
            lambda self, title, message, *a, **k: calls.append(message),
        )
        controller = make_controller(BackendRegistry(), FakeTransport(make_transport_state()))

        controller.run_first_launch_setup()

        # Reported exactly once, and names both halves of the AC: what got
        # set up (the shortcut) and how to change it (Settings).
        assert len(calls) == 1
        assert "Control+Alt+S" in calls[0]
        assert "Settings" in calls[0]

    def test_an_unimplemented_platform_is_reported_and_does_not_stop_the_app(
        self, make_controller, monkeypatch
    ):
        # AC: "a step that cannot run reports why and does not stop the app
        # from starting" -- macOS (SNX-85) has nothing behind the seam yet,
        # and any future platform in the same state must behave the same
        # way: reported, not fatal.
        monkeypatch.setattr(app.setup_desktop, "load_setup_complete", lambda cd=None: False)

        def raise_unimplemented(**kwargs):
            raise app.platform.UnimplementedPlatformError("macOS", "install_desktop_integration")

        monkeypatch.setattr(
            app.platform.current, "install_desktop_integration", raise_unimplemented
        )
        save_calls = []
        monkeypatch.setattr(
            app.setup_desktop,
            "save_setup_complete",
            lambda enabled, cd=None: save_calls.append(enabled) or True,
        )
        monkeypatch.setattr(QSystemTrayIcon, "isSystemTrayAvailable", staticmethod(lambda: True))
        calls = []
        monkeypatch.setattr(
            QSystemTrayIcon,
            "showMessage",
            lambda self, title, message, *a, **k: calls.append(message),
        )
        controller = make_controller(BackendRegistry(), FakeTransport(make_transport_state()))

        controller.run_first_launch_setup()  # must not raise

        assert len(calls) == 1
        assert "macOS" in calls[0]
        # Recorded anyway, so a future launch doesn't re-attempt (and
        # re-report) this on every single startup -- see
        # run_first_launch_setup()'s own docstring.
        assert save_calls == [True]


class TestReviewWindowIntegration:
    """The review window is off unless Settings turns it on, and opens only
    for a real capture -- never for a cancelled snip.
    """

    def _controller(self, make_controller):
        return make_controller(
            BackendRegistry([FakeCaptureBackend(make_capture_frame())]),
            FakeTransport(make_transport_state()),
            monitor_geometries=[QRectF(0, 0, 400, 300)],
        )

    def test_no_window_opens_when_the_setting_is_off(self, make_controller, monkeypatch):
        monkeypatch.setattr(overlay_module.setup_desktop, "load_after_capture", lambda cd=None: "clip")
        controller = self._controller(make_controller)
        controller.start_capture()
        controller._overlay.set_selection(QRect(10, 10, 100, 80))

        controller._overlay.copy()

        assert controller._reviews == []

    def test_a_window_opens_when_the_setting_is_on(self, make_controller, monkeypatch):
        monkeypatch.setattr(overlay_module.setup_desktop, "load_after_capture", lambda cd=None: "review")
        controller = self._controller(make_controller)
        controller.start_capture()
        controller._overlay.set_selection(QRect(10, 10, 100, 80))

        controller._overlay.copy()

        assert len(controller._reviews) == 1

    def test_cancelling_a_snip_opens_nothing(self, make_controller, monkeypatch):
        # Esc is not a capture. _on_captured fires from copy()/save() only,
        # never from the dismissal hook that every ending routes through.
        monkeypatch.setattr(overlay_module.setup_desktop, "load_after_capture", lambda cd=None: "review")
        controller = self._controller(make_controller)
        controller.start_capture()
        controller._overlay.set_selection(QRect(10, 10, 100, 80))

        controller._overlay.close()

        assert controller._reviews == []

    def test_the_setting_is_read_fresh_for_each_snip(self, make_controller, monkeypatch):
        # Toggling it in Settings should take effect on the next snip, not
        # the next launch.
        enabled = [False]
        monkeypatch.setattr(overlay_module.setup_desktop, "load_after_capture",
                            lambda cd=None: "review" if enabled[0] else "clip")
        controller = self._controller(make_controller)
        controller.start_capture()
        controller._overlay.set_selection(QRect(10, 10, 100, 80))
        controller._overlay.copy()
        assert controller._reviews == []

        enabled[0] = True
        # A whole new snip, not another copy off the stale overlay: the
        # chooser reads the setting when it opens, so that is the moment
        # the toggle has to be picked up. The first session has to end
        # first or start_capture just surfaces the overlay already open.
        controller._overlay.close()
        controller.start_capture()
        controller._overlay.set_selection(QRect(10, 10, 100, 80))
        controller._overlay.copy()

        assert len(controller._reviews) == 1

    def test_several_snips_leave_several_windows_open(self, make_controller, monkeypatch):
        monkeypatch.setattr(overlay_module.setup_desktop, "load_after_capture", lambda cd=None: "review")
        controller = self._controller(make_controller)
        controller.start_capture()
        controller._overlay.set_selection(QRect(10, 10, 100, 80))

        controller._overlay.copy()
        controller._overlay.copy()

        assert len(controller._reviews) == 2

    def test_a_closed_window_is_forgotten(self, make_controller, monkeypatch):
        # Otherwise a long session accumulates every snip it ever took.
        monkeypatch.setattr(overlay_module.setup_desktop, "load_after_capture", lambda cd=None: "review")
        controller = self._controller(make_controller)
        controller.start_capture()
        controller._overlay.set_selection(QRect(10, 10, 100, 80))
        controller._overlay.copy()

        controller._reviews[0].close()

        assert controller._reviews == []


class TestChooserKindIntegration:
    """SNX-120: the stills/record switch has to remember which side was
    last used *across snips*, not just across a `reopen()` on one live
    `Chooser` -- `start_capture` builds a whole new `OverlayWindow` (and so
    a whole new `Chooser`) each time, so nothing survives a session ending
    unless it is read from and written to disk, the same as `after_capture`
    already is.
    """

    def _controller(self, make_controller):
        return make_controller(
            BackendRegistry([FakeCaptureBackend(make_capture_frame())]),
            FakeTransport(make_transport_state()),
            monitor_geometries=[QRectF(0, 0, 400, 300)],
        )

    def test_a_fresh_snip_opens_on_the_stored_side(self, make_controller, monkeypatch):
        monkeypatch.setattr(overlay_module.setup_desktop, "load_kind", lambda cd=None: "record")
        controller = self._controller(make_controller)

        controller.start_capture()

        assert controller._overlay._chooser.kind == "record"

    def test_flipping_the_switch_saves_the_new_side(self, make_controller, monkeypatch):
        saved = []
        monkeypatch.setattr(overlay_module.setup_desktop, "load_kind", lambda cd=None: "stills")
        monkeypatch.setattr(
            overlay_module.setup_desktop, "save_kind",
            lambda value, cd=None: saved.append(value),
        )
        controller = self._controller(make_controller)
        controller.start_capture()

        controller._overlay._chooser.set_kind("record")

        assert saved == ["record"]

    def test_the_next_snip_picks_up_what_the_last_one_left(self, make_controller, monkeypatch):
        # No mock destinations here: round-trips through the real
        # load_kind/save_kind, backed by a stand-in for the module-level
        # dict a bare monkeypatch would otherwise need to fake persistence.
        store = {}
        monkeypatch.setattr(
            overlay_module.setup_desktop, "load_kind",
            lambda cd=None: store.get("kind", "stills"),
        )
        monkeypatch.setattr(
            overlay_module.setup_desktop, "save_kind",
            lambda value, cd=None: store.__setitem__("kind", value),
        )
        controller = self._controller(make_controller)
        controller.start_capture()
        controller._overlay._chooser.set_kind("record")
        controller._overlay.close()

        controller.start_capture()

        assert controller._overlay._chooser.kind == "record"


class TestQApplicationLifetime:
    """The QApplication must exist -- and stay alive -- before a transport
    claims its socket.

    QLocalServer's socket notifier binds to the thread's event dispatcher at
    construction. With no live QApplication there is no dispatcher, and
    building one afterwards orphans the notifier rather than adopting it:
    newConnection never fires again, so a resident accepts no forwarded
    requests and `snipux --snip` -- the keyboard shortcut's only caller --
    does nothing at all, silently, for the life of the process.
    """

    def test_the_qapplication_is_held_beyond_the_callers_scope(self):
        # The original bug was subtler than the ordering: _ensure_qapplication
        # built one and returned it, run_resident_app discarded the return
        # value, and PyQt collected it before try_claim() ran -- the same
        # foot-gun this file documents for parentless widgets.
        returned = app._ensure_qapplication()

        assert app._QAPPLICATION is returned
        assert QApplication.instance() is returned

    def test_it_survives_a_garbage_collection_between_use_sites(self):
        import gc

        app._ensure_qapplication()
        gc.collect()

        assert QApplication.instance() is not None

    def test_it_reuses_an_existing_instance(self):
        first = app._ensure_qapplication()
        second = app._ensure_qapplication()

        assert first is second


class TestSnipRequestProtocol:
    """A liveness probe and a capture request both connect; only one of them
    means "take a snip".
    """

    def test_a_bare_connection_is_not_a_request(self, tmp_path):
        # try_claim() probes by connecting. When a connection alone was the
        # request, every probe fired a capture on the resident -- including
        # --snip's own probe moments before its real request, which is why
        # one keypress delivered two.
        name = f"snipux-test-{tmp_path.name}"
        server = app.QLocalSocketTransport(name)
        assert server.try_claim()
        fired = []
        server.listen(lambda: fired.append(True), lambda: None)

        probe = app.QLocalSocketTransport(name)
        assert probe.try_claim() is False  # the probe connects, then gives up
        QApplication.processEvents()
        QTest.qWait(50)
        QApplication.processEvents()

        assert fired == [], "a liveness probe must not trigger a capture"

    @skip_on_windows(
        "listen()'s _accept() calls connection.waitForReadyRead() -- a "
        "nested event loop -- from inside a slot invoked by "
        "QApplication.processEvents() during newConnection. On Windows' "
        "named-pipe QLocalSocket backend that nested wait does not reliably "
        "observe bytes the client already flushed within the timeout, so "
        "the request is silently missed; this reproduces standalone and "
        "deterministically, not just under load. The Unix-domain-socket "
        "backend used on the target platform (Linux) does not have this "
        "timing gap."
    )
    def test_a_real_request_is_delivered(self, tmp_path):
        name = f"snipux-test-req-{tmp_path.name}"
        server = app.QLocalSocketTransport(name)
        assert server.try_claim()
        fired = []
        server.listen(lambda: fired.append(True), lambda: None)

        client = app.QLocalSocketTransport(name)
        client.send_snip_request()
        for _ in range(20):
            QApplication.processEvents()
            QTest.qWait(20)
            if fired:
                break

        assert fired == [True]

    @skip_on_windows(
        "Same nested-wait timing gap test_a_real_request_is_delivered "
        "documents for the snip byte -- the settings byte goes through the "
        "identical _accept() path, on the identical Windows named-pipe "
        "backend."
    )
    def test_a_settings_request_is_delivered_and_never_fires_a_snip(self, tmp_path):
        # SNX-78: --settings and --snip share one connection handler
        # (`_accept()`), dispatching on which byte arrived -- proving the
        # settings byte reaches `on_settings_request`, and only that
        # callback, is what proves the dispatch (not just the byte) is
        # correct.
        name = f"snipux-test-settings-{tmp_path.name}"
        server = app.QLocalSocketTransport(name)
        assert server.try_claim()
        snip_fired, settings_fired = [], []
        server.listen(
            lambda: snip_fired.append(True), lambda: settings_fired.append(True)
        )

        client = app.QLocalSocketTransport(name)
        client.send_settings_request()
        for _ in range(20):
            QApplication.processEvents()
            QTest.qWait(20)
            if settings_fired:
                break

        assert settings_fired == [True]
        assert snip_fired == []


class TestASecondRequestSurfacesTheOverlay:
    """A snip request while one is already open used to be a silent no-op.

    An overlay the user has lost track of then turns every later press of
    the shortcut into nothing at all, with no clue why -- indistinguishable
    from the keybinding having stopped working, and reported as exactly
    that.
    """

    def _controller(self, make_controller):
        return make_controller(
            BackendRegistry([FakeCaptureBackend(make_capture_frame())]),
            FakeTransport(make_transport_state()),
            monitor_geometries=[QRectF(0, 0, 400, 300)],
        )

    def test_it_does_not_open_a_second_overlay(self, make_controller):
        controller = self._controller(make_controller)
        controller.start_capture()
        first = controller._overlay

        controller.start_capture()

        assert controller._overlay is first

    def test_it_reveals_an_overlay_still_inside_its_opacity_delay(self, make_controller):
        # Raising something still transparent would look like nothing
        # happening, which is the whole complaint.
        controller = self._controller(make_controller)
        controller.start_capture()
        controller._overlay.setWindowOpacity(0.0)

        controller.start_capture()

        assert controller._overlay.windowOpacity() == 1.0

    def test_a_request_after_it_closes_opens_a_fresh_one(self, make_controller):
        controller = self._controller(make_controller)
        controller.start_capture()
        first = controller._overlay
        first.close()

        controller.start_capture()

        assert controller._overlay is not None
        assert controller._overlay is not first


class TestTheChoosersOutcomeWins:
    """What the user picked on the chooser a moment ago beats a setting
    they configured once.
    """

    def _controller(self, make_controller, monkeypatch, stored_review: bool):
        monkeypatch.setattr(
            overlay_module.setup_desktop, "load_after_capture",
            lambda cd=None: "review" if stored_review else "clip",
        )
        controller = make_controller(
            BackendRegistry([FakeCaptureBackend(make_capture_frame())]),
            FakeTransport(make_transport_state()),
            monitor_geometries=[QRectF(0, 0, 400, 300)],
        )
        controller.start_capture()
        controller._overlay.set_selection(QRect(10, 10, 100, 80))
        return controller

    def test_review_opens_a_window_even_when_the_setting_says_otherwise(
        self, make_controller, monkeypatch
    ):
        controller = self._controller(make_controller, monkeypatch, stored_review=False)
        controller._overlay._chooser.set_after("review")

        controller._overlay.copy()

        assert len(controller._reviews) == 1

    @pytest.mark.parametrize("outcome", ["clip", "file"])
    def test_the_other_outcomes_open_nothing(
        self, make_controller, monkeypatch, outcome
    ):
        controller = self._controller(make_controller, monkeypatch, stored_review=True)
        controller._overlay._chooser.set_after(outcome)

        controller._overlay.copy()

        assert controller._reviews == []

    def test_an_untouched_chooser_carries_what_settings_said(
        self, make_controller, monkeypatch
    ):
        # It is seeded from Settings when the overlay opens, so "untouched"
        # and "agrees with Settings" are the same state.
        controller = self._controller(make_controller, monkeypatch, stored_review=True)

        assert controller._overlay.outcome == "review"

        controller._overlay.copy()

        assert len(controller._reviews) == 1


class _FakeScreen:
    """Stand-in for QScreen exposing only what `_real_monitor_geometries()`
    calls -- `geometry()` -- mirroring test_capture.py's `_FakeScreen` for
    the same reason: a monitor above-and-left of the primary, or two
    monitors at different scale factors, isn't reachable on the single real
    display this suite normally runs under, so both are faked here instead.
    """

    def __init__(self, geometry: QRect):
        self._geometry = geometry

    def geometry(self) -> QRect:
        return self._geometry


class _FakeQGuiApplication:
    """Stand-in for the QGuiApplication class object itself -- see
    test_capture.py's identical pattern. `_real_monitor_geometries()` calls
    `QGuiApplication.screens()` the same class-style way capture.py does,
    so monkeypatching the module's `QGuiApplication` name to an instance of
    this works the same way attribute lookup would on the real class.
    """

    def __init__(self, screens):
        self._screens = screens

    def screens(self):
        return self._screens


class TestRealMonitorGeometries:
    """SNX-89: `_real_monitor_geometries()` is what feeds the overlay's
    per-monitor chrome/veil placement (`AppController.start_capture`) when
    no synthetic list is injected. On Linux this is Qt's own screen list,
    which also works on Windows -- CLAUDE.md's per-monitor-DPI and
    negative-coordinate warnings are about what that list can *contain* on
    Windows, not about needing Windows-specific code to read it, so these
    tests fake `QGuiApplication.screens()` rather than exercise any
    platform-specific path.
    """

    def test_covers_every_screen_including_one_above_and_left(
        self, make_controller, monkeypatch
    ):
        # Mirrors the real three-monitor Windows desktop
        # TestQtNativeWindowsBackend (test_capture.py) was verified
        # against: one screen right of the primary, one above-and-left of
        # it -- negative x *and* negative y in the same virtual desktop.
        screens = [
            _FakeScreen(QRect(0, 0, 2560, 1440)),
            _FakeScreen(QRect(2560, 0, 2560, 1440)),
            _FakeScreen(QRect(1164, -1440, 2560, 1440)),
        ]
        monkeypatch.setattr(app, "QGuiApplication", _FakeQGuiApplication(screens))
        controller = make_controller(BackendRegistry(), FakeTransport(make_transport_state()))

        geometries = controller._real_monitor_geometries()

        assert geometries == [
            QRectF(0, 0, 2560, 1440),
            QRectF(2560, 0, 2560, 1440),
            QRectF(1164, -1440, 2560, 1440),
        ]

    def test_covers_two_monitors_at_different_scale_factors(
        self, make_controller, monkeypatch
    ):
        # Qt already reports each screen's geometry divided by its own
        # devicePixelRatio, so two physically-1920x1080 panels -- one at
        # 100% scale, one at 200% -- come back with different logical
        # sizes even though `_real_monitor_geometries()` is nothing more
        # than a pass-through of Qt's own screen list.
        screens = [
            _FakeScreen(QRect(0, 0, 1920, 1080)),  # 100% scale
            _FakeScreen(QRect(1920, 0, 960, 540)),  # 200% scale, same panel size
        ]
        monkeypatch.setattr(app, "QGuiApplication", _FakeQGuiApplication(screens))
        controller = make_controller(BackendRegistry(), FakeTransport(make_transport_state()))

        geometries = controller._real_monitor_geometries()

        assert geometries == [QRectF(0, 0, 1920, 1080), QRectF(1920, 0, 960, 540)]


def make_scaled_capture_frame(
    logical_origin: QPointF, logical_size: QSizeF, ratio: float, fill_color=FILL_COLOR
) -> Frame:
    """A synthetic `Frame` whose image carries more pixels than its logical
    size, the same way a real capture does on any display with a device
    pixel ratio above one -- see `Frame`'s own docstring. Used below to
    prove the overlay's selection/export/chrome-placement math is correct
    against a scaled, negative-origin frame without needing real HiDPI or
    multi-monitor Windows hardware to reach it.
    """
    image = QImage(
        round(logical_size.width() * ratio),
        round(logical_size.height() * ratio),
        QImage.Format.Format_RGB32,
    )
    image.fill(fill_color)
    return Frame(image=image, logical_origin=logical_origin, logical_size=logical_size)


class TestWindowsShapedMultiMonitorSelection:
    """SNX-89: the overlay covers a Windows-shaped multi-monitor desktop --
    a monitor above-and-left of the primary (negative logical coordinates)
    and neighbouring monitors at different scale factors -- correctly, and
    none of this needs real Windows hardware to prove: a synthetic scaled
    `Frame` plus injected `monitor_geometries` exercises exactly the same
    code `start_capture()` wires a real capture through.
    """

    def test_selection_on_a_negative_origin_monitor_does_not_drift(self, make_controller):
        # Same three-monitor layout as TestRealMonitorGeometries above and
        # TestQtNativeWindowsBackend (test_capture.py): virtual desktop
        # union is QRectF(0, -1440, 5120, 2880). The frame's image carries
        # 2x the logical pixels, the same as a real capture would on a
        # scaled display (Frame's per-axis scale, not a single assumed
        # factor, is what `Frame.crop()`/`render_selection()` already use
        # to map between the two -- see their own docstrings).
        monitor_geometries = [
            QRectF(0, 0, 2560, 1440),
            QRectF(2560, 0, 2560, 1440),
            QRectF(1164, -1440, 2560, 1440),  # above-and-left of the primary
        ]
        frame = make_scaled_capture_frame(QPointF(0, -1440), QSizeF(5120, 2880), ratio=2.0)
        controller = make_controller(
            BackendRegistry([FakeCaptureBackend(frame)]),
            FakeTransport(make_transport_state()),
            monitor_geometries=monitor_geometries,
        )
        controller.start_capture()
        overlay = controller._overlay

        # A selection entirely on the negative-origin monitor: absolute
        # QRectF(1300, -1200, 400, 300), which in this window's own
        # coordinates (absolute minus the frame's logical origin) is
        # QRect(1300, 240, 400, 300).
        selection = QRect(1300, 240, 400, 300)
        overlay.set_selection(selection)

        # No drift: the selection's own absolute point resolves to the
        # negative-origin monitor, not the primary or its neighbour --
        # this is exactly what `_chrome_bounds()` relies on to keep the
        # floating bar on the monitor the user is actually pointing at.
        absolute_point = overlay._to_absolute(QPointF(selection.center()))
        assert overlay._monitor_at(absolute_point) == QRectF(1164, -1440, 2560, 1440)

        # The dimension chip reports logical size, matching the behaviour
        # already required on Linux -- never the frame's larger pixel size.
        size_text, _mark_text = overlay._dimension_chip_texts()
        assert size_text == "400 × 300"

        # The exported image is at native resolution (2x here), not
        # downscaled to the selection's logical size.
        rendered = overlay.rendered_image()
        assert rendered.width() == 800
        assert rendered.height() == 600

    def test_selections_on_neighbouring_monitors_at_different_scales_stay_on_their_own_monitor(
        self, make_controller
    ):
        # Monitor A: 1920x1080 logical at 100% scale. Monitor B: 960x540
        # logical at 200% scale (same physical panel size, half the
        # logical footprint) -- placed directly beside A, per CLAUDE.md's
        # warning that two displays can carry different scale factors at
        # once. The frame's own image is still one uniform 2x composite
        # (Frame/crop() can only represent one ratio for the whole image --
        # see QtNativeX11Backend's own comment on why per-monitor DPI needs
        # a Frame-level model change that is out of scope here); what this
        # proves is that the *selection* math -- purely logical-coordinate,
        # decoupled from the frame's pixel resolution -- never drifts or
        # bleeds across the boundary between two differently-scaled
        # neighbours.
        monitor_a = QRectF(0, 0, 1920, 1080)
        monitor_b = QRectF(1920, 0, 960, 540)
        frame = make_scaled_capture_frame(QPointF(0, 0), QSizeF(2880, 1080), ratio=2.0)
        controller = make_controller(
            BackendRegistry([FakeCaptureBackend(frame)]),
            FakeTransport(make_transport_state()),
            monitor_geometries=[monitor_a, monitor_b],
        )
        controller.start_capture()
        overlay = controller._overlay

        # Entirely on A, close to the shared boundary.
        overlay.set_selection(QRect(1800, 100, 100, 100))
        assert overlay._monitor_at(overlay._to_absolute(QPointF(1850, 150))) == monitor_a

        # Entirely on B, close to the same boundary from the other side.
        selection_b = QRect(2000, 50, 300, 200)
        overlay.set_selection(selection_b)
        assert overlay._monitor_at(overlay._to_absolute(QPointF(2150, 150))) == monitor_b

        size_text, _mark_text = overlay._dimension_chip_texts()
        assert size_text == "300 × 200"
        rendered = overlay.rendered_image()
        assert rendered.width() == 600
        assert rendered.height() == 400
