"""SNX-85: the platform seam -- installing/removing desktop integration,
binding/unbinding the global shortcut, and reporting where a saved image
should go, gathered behind one interface (`snipux.platform.Platform`) and
selected by `sys.platform` at import time.

`LinuxPlatform` is a thin adapter onto `snipux.setup_desktop`, which already
has its own, much larger test suite (test_setup_desktop.py) covering the
actual `.desktop`/gsettings/XDG behaviour -- these tests only prove the
adapter forwards to the right function with the right arguments and hands
back what it returned, not that behaviour again.

`TestWindowsPlatform`/`TestHotkeyEventFilter` (SNX-91) cover
`WindowsPlatform.bind_shortcut`/`unbind_shortcut` for real, now that they
are no longer stubs -- against a fake `ctypes.windll.user32`, the same
`_patch_win32_dll`-style pattern test_capture.py already uses for
`Win32GdiBackend`/`WindowsWindowGeometryProvider`, rather than actually
grabbing a system-wide hotkey on whatever machine runs the suite.

`TestCreateShortcut`/`TestWriteAndRemoveIcon`/`TestWindowsDesktopIntegration`
(SNX-92) cover `WindowsPlatform.install_desktop_integration`/
`remove_desktop_integration` for real. `_create_shortcut` (the COM
`IShellLinkW`/`IPersistFile` call) is faked at the vtable itself
(`_FakeShellLinkCom`), the COM counterpart of `_FakeUser32Hotkey` above;
`install_desktop_integration`/`remove_desktop_integration`'s own tests fake
`_create_shortcut` wholesale instead, the same "prove the orchestration
reaches the real mechanism, which has its own tests" split
`TestLinuxPlatform` already draws around `setup_desktop.run_setup`.
"""

import ctypes
import importlib
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from PyQt6.QtCore import QRect

import snipux.platform as platform_pkg
from snipux import setup_desktop
from snipux.platform import Platform, UnimplementedPlatformError, darwin, linux, windows


@pytest.fixture(autouse=True)
def _restore_the_real_platform():
    """Every test in `TestPlatformSelection` reloads `snipux.platform` with
    a fake `sys.platform` to exercise `_select()`'s branches -- reloading it
    once more here, after `monkeypatch` has already put the real
    `sys.platform` back, is what stops that fake selection from leaking into
    whatever test (in this file or another) runs next and reaches
    `snipux.platform.current`.
    """
    yield
    importlib.reload(platform_pkg)


class TestPlatformSelection:
    """AC: a platform package selects an implementation from sys.platform
    at import time.
    """

    def test_selects_linux_on_a_linux_sys_platform(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "linux")

        reloaded = importlib.reload(platform_pkg)

        assert isinstance(reloaded.current, linux.LinuxPlatform)

    def test_selects_linux_on_a_versioned_linux_sys_platform(self, monkeypatch):
        # Older interpreters reported "linux2"/"linux3" rather than the
        # bare "linux" every current CPython uses -- matched with a prefix
        # check for exactly that reason.
        monkeypatch.setattr(sys, "platform", "linux2")

        reloaded = importlib.reload(platform_pkg)

        assert isinstance(reloaded.current, linux.LinuxPlatform)

    def test_selects_windows_on_win32(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "win32")

        reloaded = importlib.reload(platform_pkg)

        assert isinstance(reloaded.current, windows.WindowsPlatform)

    def test_selects_darwin_on_darwin(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "darwin")

        reloaded = importlib.reload(platform_pkg)

        assert isinstance(reloaded.current, darwin.DarwinPlatform)

    def test_raises_plainly_on_an_unrecognised_platform(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "some-made-up-os")

        with pytest.raises(RuntimeError, match="some-made-up-os"):
            importlib.reload(platform_pkg)


class TestLinuxPlatform:
    """`LinuxPlatform` forwards each operation to `setup_desktop`'s existing
    (separately-tested) implementation and returns exactly what it reports.
    """

    def test_install_desktop_integration_delegates_to_run_setup(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            setup_desktop, "run_setup", lambda **kwargs: calls.append(kwargs) or 0
        )

        result = linux.LinuxPlatform().install_desktop_integration(shortcut="Alt+Print")

        assert result == 0
        assert calls == [{"shortcut": "Alt+Print"}]

    def test_install_desktop_integration_propagates_a_nonzero_exit_code(self, monkeypatch):
        monkeypatch.setattr(setup_desktop, "run_setup", lambda **kwargs: 1)

        assert linux.LinuxPlatform().install_desktop_integration() == 1

    def test_remove_desktop_integration_delegates_to_run_remove(self, monkeypatch):
        calls = []
        monkeypatch.setattr(setup_desktop, "run_remove", lambda: calls.append("called") or 0)

        result = linux.LinuxPlatform().remove_desktop_integration()

        assert result == 0
        assert calls == ["called"]

    def test_bind_shortcut_binds_via_the_located_console_script(self, monkeypatch, tmp_path):
        exec_path = tmp_path / "snipux"
        monkeypatch.setattr(setup_desktop, "find_console_script", lambda: exec_path)
        calls = []
        monkeypatch.setattr(
            setup_desktop,
            "bind_gnome_shortcut",
            lambda path, shortcut=None: calls.append((path, shortcut)) or "bound it",
        )

        result = linux.LinuxPlatform().bind_shortcut("Alt+Print")

        assert result == "bound it"
        assert calls == [(exec_path, "Alt+Print")]

    def test_bind_shortcut_defaults_to_the_remembered_shortcut(self, monkeypatch, tmp_path):
        # No shortcut passed in -- bind_gnome_shortcut's own None default
        # (fall back to load_shortcut()) must reach it untouched.
        exec_path = tmp_path / "snipux"
        monkeypatch.setattr(setup_desktop, "find_console_script", lambda: exec_path)
        calls = []
        monkeypatch.setattr(
            setup_desktop,
            "bind_gnome_shortcut",
            lambda path, shortcut=None: calls.append(shortcut) or "bound it",
        )

        linux.LinuxPlatform().bind_shortcut()

        assert calls == [None]

    def test_bind_shortcut_reports_plainly_when_the_console_script_cannot_be_found(
        self, monkeypatch
    ):
        monkeypatch.setattr(setup_desktop, "find_console_script", lambda: None)

        result = linux.LinuxPlatform().bind_shortcut()

        assert "could not be found" in result
        assert "not re-bound" in result

    def test_unbind_shortcut_delegates_to_unbind_gnome_shortcut(self, monkeypatch):
        monkeypatch.setattr(setup_desktop, "unbind_gnome_shortcut", lambda: "removed it")

        assert linux.LinuxPlatform().unbind_shortcut() == "removed it"

    def test_default_save_folder_delegates_to_setup_desktop(self, monkeypatch, tmp_path):
        monkeypatch.setattr(setup_desktop, "default_save_folder", lambda: tmp_path)

        assert linux.LinuxPlatform().default_save_folder() == tmp_path

    def test_ensure_stable_install_is_a_noop_by_default(self):
        # SNX-103: only WindowsPlatform overrides this -- a Linux install
        # (a source checkout, or pip/pipx, neither of which sets
        # `sys.frozen`) is already in a location a package manager
        # manages, so `Platform`'s base implementation is what runs here.
        assert linux.LinuxPlatform().ensure_stable_install() is None


class _FakeScreen:
    """Just the two rects `reserved_top` reads off a `QScreen`."""

    def __init__(self, geometry, available=None):
        self._geometry = geometry
        self._available = available if available is not None else geometry

    def geometry(self):
        return self._geometry

    def availableGeometry(self):
        return self._available


class TestReservedTop:
    """How much of a monitor's top edge the desktop's own chrome owns.

    Chrome placement only -- the capture is untouched by this. Zero is the
    safe answer everywhere, which is what every platform without an
    override returns.
    """

    def _linux(self, monkeypatch, *, session="x11", qt_platform="xcb", xprop=None):
        monkeypatch.setattr(linux.capture, "detect_session_type", lambda: session)
        monkeypatch.setattr(linux.QGuiApplication, "platformName", staticmethod(lambda: qt_platform))
        if xprop is not None:
            monkeypatch.setattr(
                linux.subprocess,
                "run",
                lambda *a, **k: SimpleNamespace(stdout=xprop),
            )
        return linux.LinuxPlatform()

    def test_the_portable_answer_is_the_gap_qt_already_reports(self):
        # Windows and macOS inherit this: Qt is told the truth there.
        screen = _FakeScreen(QRect(0, 0, 1920, 1080), QRect(0, 40, 1920, 1040))

        assert windows.WindowsPlatform().reserved_top(screen) == 40
        assert darwin.DarwinPlatform().reserved_top(screen) == 40

    def test_no_gap_reported_is_no_inset(self):
        screen = _FakeScreen(QRect(0, 0, 1920, 1080))

        assert windows.WindowsPlatform().reserved_top(screen) == 0

    def test_x11_falls_back_to_the_work_area_qt_did_not_pass_on(self, monkeypatch):
        # The measured case: GNOME reserves 32px and Qt reports none of it.
        platform_impl = self._linux(
            monkeypatch, xprop="_NET_WORKAREA(CARDINAL) = 0, 32, 6400, 1337\n"
        )
        screen = _FakeScreen(QRect(1920, 0, 2560, 1440))

        assert platform_impl.reserved_top(screen) == 32

    def test_a_monitor_mounted_below_the_desktops_top_edge_is_already_clear(self, monkeypatch):
        # The work area is one rect for the whole virtual desktop, so a
        # monitor hung lower than its top is past the bar by definition.
        platform_impl = self._linux(
            monkeypatch, xprop="_NET_WORKAREA(CARDINAL) = 0, 32, 6400, 1337\n"
        )

        assert platform_impl.reserved_top(_FakeScreen(QRect(0, 201, 1920, 1080))) == 0

    def test_qt_having_an_answer_already_wins_without_shelling_out(self, monkeypatch):
        def fail(*a, **k):
            raise AssertionError("xprop must not run when Qt already knows")

        monkeypatch.setattr(linux.subprocess, "run", fail)
        platform_impl = self._linux(monkeypatch)

        screen = _FakeScreen(QRect(0, 0, 1920, 1080), QRect(0, 27, 1920, 1053))
        assert platform_impl.reserved_top(screen) == 27

    def test_wayland_asks_nothing_and_reserves_nothing(self, monkeypatch):
        # `show_on_screen` fullscreens the overlay onto one output there,
        # and GNOME hides its top bar for a fullscreen window -- watched
        # happen on a real GNOME Wayland session for SNX-110.
        def fail(*a, **k):
            raise AssertionError("there is no _NET_WORKAREA on Wayland")

        monkeypatch.setattr(linux.subprocess, "run", fail)
        platform_impl = self._linux(monkeypatch, session="wayland")

        assert platform_impl.reserved_top(_FakeScreen(QRect(0, 0, 1920, 1080))) == 0

    def test_wayland_reads_a_real_reservation_rather_than_assuming_none(self, monkeypatch):
        # SNX-110 AC: "if GNOME does reserve space on Wayland, it is read
        # rather than assumed away." There is no `_NET_WORKAREA` to shell
        # out for on Wayland, but Qt's own `availableGeometry()` is still
        # asked (the `portable` line runs unconditionally) -- so a
        # compositor that did carve out top-edge space would already show
        # up here, not get flattened to 0.
        def fail(*a, **k):
            raise AssertionError("there is no _NET_WORKAREA on Wayland")

        monkeypatch.setattr(linux.subprocess, "run", fail)
        platform_impl = self._linux(monkeypatch, session="wayland")
        screen = _FakeScreen(QRect(0, 0, 1920, 1080), QRect(0, 32, 1920, 1048))

        assert platform_impl.reserved_top(screen) == 32

    def test_an_offscreen_qt_platform_asks_nothing(self, monkeypatch):
        # The headless suite runs inside a real X11 login session, so
        # `XDG_SESSION_TYPE` says x11 while nothing is painting over
        # anything. Without this the result would depend on whether the
        # developer running the suite has a GNOME bar.
        def fail(*a, **k):
            raise AssertionError("no shell to hide behind under offscreen")

        monkeypatch.setattr(linux.subprocess, "run", fail)
        platform_impl = self._linux(monkeypatch, qt_platform="offscreen")

        assert platform_impl.reserved_top(_FakeScreen(QRect(0, 0, 1920, 1080))) == 0

    @pytest.mark.parametrize(
        "stdout", ["", "_NET_WORKAREA(CARDINAL) = \n", "nonsense", "_NET_WORKAREA = a, b, c, d\n"]
    )
    def test_an_unreadable_answer_reserves_nothing(self, monkeypatch, stdout):
        platform_impl = self._linux(monkeypatch, xprop=stdout)

        assert platform_impl.reserved_top(_FakeScreen(QRect(0, 0, 1920, 1080))) == 0

    def test_a_missing_xprop_reserves_nothing(self, monkeypatch):
        # Same "degrade, never raise" rule the wmctrl-backed window
        # provider already follows.
        def missing(*a, **k):
            raise FileNotFoundError("xprop")

        monkeypatch.setattr(linux.subprocess, "run", missing)
        platform_impl = self._linux(monkeypatch)

        assert platform_impl.reserved_top(_FakeScreen(QRect(0, 0, 1920, 1080))) == 0


class TestStubPlatforms:
    """AC: macOS's implementation exists and raises a clear error naming the
    platform and the operation that isn't implemented yet. Windows'
    `bind_shortcut`/`unbind_shortcut` (SNX-91) and
    `install_desktop_integration`/`remove_desktop_integration` (SNX-92) are
    real now -- see `TestWindowsPlatform`/`TestWindowsDesktopIntegration`
    below -- so it keeps only the operation still unimplemented there.
    """

    DARWIN_OPERATIONS = (
        "install_desktop_integration",
        "remove_desktop_integration",
        "bind_shortcut",
        "unbind_shortcut",
        "default_save_folder",
    )

    WINDOWS_UNIMPLEMENTED_OPERATIONS = ("default_save_folder",)

    def test_every_darwin_operation_raises_naming_the_platform_and_the_operation(self):
        stub = darwin.DarwinPlatform()

        for operation in self.DARWIN_OPERATIONS:
            with pytest.raises(UnimplementedPlatformError) as excinfo:
                getattr(stub, operation)()
            message = str(excinfo.value)
            assert "macOS" in message
            assert operation in message

    def test_every_unimplemented_windows_operation_raises_naming_the_platform_and_the_operation(
        self,
    ):
        stub = windows.WindowsPlatform()

        for operation in self.WINDOWS_UNIMPLEMENTED_OPERATIONS:
            with pytest.raises(UnimplementedPlatformError) as excinfo:
                getattr(stub, operation)()
            message = str(excinfo.value)
            assert "Windows" in message
            assert operation in message

    def test_stubs_still_satisfy_the_platform_interface(self):
        # Neither stub can be constructed at all unless every abstract
        # method of Platform is implemented -- proves the stub is a real,
        # complete implementation of the seam rather than a partial one
        # that just happens not to be exercised yet.
        assert isinstance(windows.WindowsPlatform(), Platform)
        assert isinstance(darwin.DarwinPlatform(), Platform)


class _FakeUser32Hotkey:
    """Stand-in for `ctypes.windll.user32`, covering only what
    `WindowsPlatform.bind_shortcut`/`unbind_shortcut` call:
    `RegisterHotKey`/`UnregisterHotKey`. `already_registered_ids` simulates
    another application already owning a given (modifiers, vk) pair --
    `RegisterHotKey` returns 0 and a real `GetLastError()` would report
    `ERROR_HOTKEY_ALREADY_REGISTERED` (1409) for it, so this fake reports
    the same error code windows.py actually reads.
    """

    _ERROR_HOTKEY_ALREADY_REGISTERED = 1409

    def __init__(self, clashing=(), fail_unregister=False):
        self._clashing = set(clashing)
        self._fail_unregister = fail_unregister
        self.registered: list[tuple] = []
        self.unregistered: list[int] = []
        self.last_error = 0

    def RegisterHotKey(self, hwnd, hotkey_id, modifiers, vk):
        if (modifiers, vk) in self._clashing:
            self.last_error = self._ERROR_HOTKEY_ALREADY_REGISTERED
            return 0
        self.registered.append((hotkey_id, modifiers, vk))
        self.last_error = 0
        return 1

    def UnregisterHotKey(self, hwnd, hotkey_id):
        self.unregistered.append(hotkey_id)
        return 0 if self._fail_unregister else 1


def _patch_windows_hotkey_dll(monkeypatch, user32):
    """Points `windows.ctypes.windll.user32`/`.GetLastError` at the given
    fake -- `raising=False` because `ctypes.windll` doesn't exist at all off
    Windows, and this suite runs on both (mirrors test_capture.py's
    `_patch_win32_dll`).
    """
    monkeypatch.setattr(windows.ctypes, "windll", SimpleNamespace(user32=user32), raising=False)
    monkeypatch.setattr(windows.ctypes, "GetLastError", lambda: user32.last_error, raising=False)


class TestWindowsPlatform:
    """AC: the resident app registers a global hotkey on Windows, defaulting
    to Ctrl+Alt+S; a clash with another application's own hotkey is reported
    by name rather than swallowed; changing the shortcut re-registers it
    (releasing the old one first) rather than holding both.
    """

    _MOD_CONTROL = 0x0002
    _MOD_ALT = 0x0001
    _MOD_NOREPEAT = 0x4000
    _VK_S = ord("S")

    def test_binds_the_default_shortcut(self, monkeypatch, tmp_path):
        monkeypatch.setattr(setup_desktop, "config_path", lambda config_dir=None: tmp_path / "x")
        user32 = _FakeUser32Hotkey()
        _patch_windows_hotkey_dll(monkeypatch, user32)

        result = windows.WindowsPlatform().bind_shortcut()

        assert "Bound Control+Alt+S" in result
        [(hotkey_id, modifiers, vk)] = user32.registered
        assert vk == self._VK_S
        assert modifiers == self._MOD_CONTROL | self._MOD_ALT | self._MOD_NOREPEAT

    def test_bind_shortcut_accepts_an_explicit_shortcut(self, monkeypatch):
        user32 = _FakeUser32Hotkey()
        _patch_windows_hotkey_dll(monkeypatch, user32)

        result = windows.WindowsPlatform().bind_shortcut("Shift+F9")

        assert "Bound Shift+F9" in result
        [(_id, modifiers, vk)] = user32.registered
        assert modifiers == 0x0004 | self._MOD_NOREPEAT  # MOD_SHIFT
        assert vk == 0x78  # VK_F9

    def test_records_the_bound_shortcut_on_success(self, monkeypatch):
        user32 = _FakeUser32Hotkey()
        _patch_windows_hotkey_dll(monkeypatch, user32)
        platform_ = windows.WindowsPlatform()

        platform_.bind_shortcut("Control+Alt+S")

        assert platform_.registered_shortcut == "Control+Alt+S"

    def test_a_clash_is_reported_by_name_rather_than_swallowed(self, monkeypatch):
        modifiers = self._MOD_CONTROL | self._MOD_ALT | self._MOD_NOREPEAT
        user32 = _FakeUser32Hotkey(clashing={(modifiers, self._VK_S)})
        _patch_windows_hotkey_dll(monkeypatch, user32)
        platform_ = windows.WindowsPlatform()

        result = platform_.bind_shortcut("Control+Alt+S")

        assert "Control+Alt+S" in result
        assert "already in use" in result
        # Never raised (RegisterHotKey failing is documented, expected
        # behaviour for a real clash, not a bug) and nothing is left
        # thinking it holds a registration it doesn't.
        assert platform_.registered_shortcut is None

    def test_rebinding_releases_the_previous_shortcut_first(self, monkeypatch):
        user32 = _FakeUser32Hotkey()
        _patch_windows_hotkey_dll(monkeypatch, user32)
        platform_ = windows.WindowsPlatform()
        platform_.bind_shortcut("Control+Alt+S")

        platform_.bind_shortcut("Control+Alt+X")

        assert user32.unregistered == [1]
        assert [vk for _id, _mod, vk in user32.registered] == [ord("S"), ord("X")]
        assert platform_.registered_shortcut == "Control+Alt+X"

    def test_an_unmappable_key_is_reported_without_touching_the_dll(self, monkeypatch):
        user32 = _FakeUser32Hotkey()
        _patch_windows_hotkey_dll(monkeypatch, user32)

        result = windows.WindowsPlatform().bind_shortcut("Control+Alt+PrintThisIsNotAKey")

        assert "not a key combination" in result
        assert user32.registered == []

    def test_an_invalid_shortcut_is_reported_without_touching_the_dll(self, monkeypatch):
        user32 = _FakeUser32Hotkey()
        _patch_windows_hotkey_dll(monkeypatch, user32)

        # No modifier at all -- validate_shortcut() rejects it before this
        # ever reaches RegisterHotKey, the same "a bare key would swallow
        # that key desktop-wide" reasoning setup_desktop.py already states.
        result = windows.WindowsPlatform().bind_shortcut("S")

        assert "Could not bind" in result
        assert user32.registered == []

    def test_unbind_when_nothing_is_registered_does_not_touch_the_dll(self, monkeypatch):
        user32 = _FakeUser32Hotkey()
        _patch_windows_hotkey_dll(monkeypatch, user32)

        result = windows.WindowsPlatform().unbind_shortcut()

        assert "No Snipux shortcut is currently registered" in result
        assert user32.unregistered == []

    def test_unbind_releases_a_registered_shortcut(self, monkeypatch):
        user32 = _FakeUser32Hotkey()
        _patch_windows_hotkey_dll(monkeypatch, user32)
        platform_ = windows.WindowsPlatform()
        platform_.bind_shortcut("Control+Alt+S")

        result = platform_.unbind_shortcut()

        assert "Released Control+Alt+S" in result
        assert user32.unregistered == [1]
        assert platform_.registered_shortcut is None

    def test_unbind_failure_is_reported_rather_than_raised(self, monkeypatch):
        user32 = _FakeUser32Hotkey(fail_unregister=True)
        _patch_windows_hotkey_dll(monkeypatch, user32)
        platform_ = windows.WindowsPlatform()
        platform_.bind_shortcut("Control+Alt+S")

        result = platform_.unbind_shortcut()

        assert "Could not release" in result


class TestFindShortcutConflict:
    """SNX-93: the Windows answer to `setup_desktop.
    find_shortcut_conflicts_named()`'s GNOME-only check, which Settings'
    conflict banner and Save button both call on Windows instead. AC: the
    Windows Snipping Tool's own Win+Shift+S is named without needing to
    touch the DLL at all; anything else is only visible by actually
    probing `RegisterHotKey`, which must always release what it just
    grabbed rather than leaving it held.
    """

    _MOD_CONTROL = 0x0002
    _MOD_ALT = 0x0001
    _MOD_NOREPEAT = 0x4000
    _VK_S = ord("S")
    _PROBE_ID = 2

    def test_the_snipping_tools_own_shortcut_is_named_without_touching_the_dll(
        self, monkeypatch
    ):
        user32 = _FakeUser32Hotkey()
        _patch_windows_hotkey_dll(monkeypatch, user32)

        result = windows.WindowsPlatform().find_shortcut_conflict("Super+Shift+S")

        assert result == "the Windows Snipping Tool"
        assert user32.registered == []

    def test_a_shortcut_another_application_holds_is_reported_by_name(self, monkeypatch):
        modifiers = self._MOD_CONTROL | self._MOD_ALT | self._MOD_NOREPEAT
        user32 = _FakeUser32Hotkey(clashing={(modifiers, self._VK_S)})
        _patch_windows_hotkey_dll(monkeypatch, user32)

        result = windows.WindowsPlatform().find_shortcut_conflict("Control+Alt+S")

        assert result == "another application"

    def test_a_free_shortcut_is_reported_as_none(self, monkeypatch):
        user32 = _FakeUser32Hotkey()
        _patch_windows_hotkey_dll(monkeypatch, user32)

        result = windows.WindowsPlatform().find_shortcut_conflict("Control+Alt+S")

        assert result is None

    def test_probing_a_free_shortcut_releases_it_again(self, monkeypatch):
        # A pure check, not a bind -- it must never actually hold the key.
        user32 = _FakeUser32Hotkey()
        _patch_windows_hotkey_dll(monkeypatch, user32)

        windows.WindowsPlatform().find_shortcut_conflict("Control+Alt+S")

        assert [id_ for id_, _mod, _vk in user32.registered] == [self._PROBE_ID]
        assert user32.unregistered == [self._PROBE_ID]

    def test_the_probe_never_collides_with_snipuxs_own_held_registration(self, monkeypatch):
        # The real, held registration uses _HOTKEY_ID (1); the probe must
        # use a different id so checking some other candidate never
        # unregisters the shortcut snipux is actually holding.
        user32 = _FakeUser32Hotkey()
        _patch_windows_hotkey_dll(monkeypatch, user32)
        platform_ = windows.WindowsPlatform()
        platform_.bind_shortcut("Control+Alt+S")

        platform_.find_shortcut_conflict("Shift+F9")

        # Only the probe (id 2) was released; the real registration (id 1)
        # was never touched.
        assert user32.unregistered == [self._PROBE_ID]
        assert platform_.registered_shortcut == "Control+Alt+S"

    def test_the_shortcut_snipux_already_holds_is_not_a_conflict_with_itself(
        self, monkeypatch
    ):
        # Without this check, probing the combination snipux is already
        # bound to would find its own live registration and misreport it
        # as taken by "another application".
        user32 = _FakeUser32Hotkey()
        _patch_windows_hotkey_dll(monkeypatch, user32)
        platform_ = windows.WindowsPlatform()
        platform_.bind_shortcut("Control+Alt+S")
        registered_before = list(user32.registered)

        result = platform_.find_shortcut_conflict("Control+Alt+S")

        assert result is None
        # No probe registration was even attempted.
        assert user32.registered == registered_before

    def test_an_unmappable_key_is_reported_without_touching_the_dll(self, monkeypatch):
        user32 = _FakeUser32Hotkey()
        _patch_windows_hotkey_dll(monkeypatch, user32)

        result = windows.WindowsPlatform().find_shortcut_conflict(
            "Control+Alt+PrintThisIsNotAKey"
        )

        assert result is None
        assert user32.registered == []

    def test_not_a_shortcut_at_all_is_reported_without_touching_the_dll(self, monkeypatch):
        user32 = _FakeUser32Hotkey()
        _patch_windows_hotkey_dll(monkeypatch, user32)

        result = windows.WindowsPlatform().find_shortcut_conflict("")

        assert result is None
        assert user32.registered == []


class TestGuid:
    """`_guid()` -- the one piece of `_create_shortcut` that has nothing to
    do with COM itself, exercised directly rather than only indirectly
    through a real/faked `CoCreateInstance` call.
    """

    def test_parses_a_clsid_literal_into_its_fields(self):
        # CLSID_ShellLink, byte for byte against shobjidl_core.h.
        guid = windows._guid("{00021401-0000-0000-C000-000000000046}")

        assert guid.data1 == 0x00021401
        assert guid.data2 == 0x0000
        assert guid.data3 == 0x0000
        assert bytes(guid.data4) == bytes.fromhex("C000000000000046")


class _FakeShellLinkCom:
    """A COM object built as an actual vtable-shaped block of function
    pointers (`ctypes` callback trampolines into this object's own
    methods), not a plain Python double -- `_create_shortcut` talks to it
    exactly the way it would talk to the real `IShellLinkW`/`IPersistFile`:
    by reading `interface[0][index]` and calling through it
    (`windows._com_call`). Faking it at that level is what proves
    `_create_shortcut`'s vtable slot indices are the real COM layout and
    not off by one, the same reason test_capture.py's `_FakeUser32Windows`
    replaces `EnumWindows` itself rather than mocking
    `WindowsWindowGeometryProvider.list_windows`.

    `fail_save` simulates `IPersistFile.Save` itself failing (e.g. a
    read-only Start Menu folder) -- every call up to that point still
    succeeds, matching what a real HRESULT failure there looks like.
    """

    _HRESULT = ctypes.c_long

    def __init__(self, fail_save=False):
        self.path = None
        self.description = None
        self.icon_location = None
        self.saved_to = None
        self.released = []
        self._fail_save = fail_save
        self._keepalive = []  # ctypes callback trampolines must outlive the calls
        self.shell_link = self._build_shell_link_vtable()
        self.persist_file = self._build_persist_file_vtable()

    def _vtable(self, size, slots):
        entries = [0] * size
        for index, func in slots.items():
            self._keepalive.append(func)
            entries[index] = ctypes.cast(func, ctypes.c_void_p).value
        vtable_array = (ctypes.c_void_p * size)(*entries)
        instance = (ctypes.c_void_p * 1)(ctypes.cast(vtable_array, ctypes.c_void_p))
        self._keepalive.extend([vtable_array, instance])
        return ctypes.cast(instance, ctypes.c_void_p)

    def _build_shell_link_vtable(self):
        # ctypes.CFUNCTYPE, not the real stdcall ctypes.WINFUNCTYPE: the
        # latter is only defined under sys.platform == "win32" in the
        # stdlib itself, so building this vtable with it would blow up
        # constructing the fake before a test ever reaches
        # _patch_windows_com() to patch anything -- and _patch_windows_com()
        # already points windows.ctypes.WINFUNCTYPE at ctypes.CFUNCTYPE for
        # every platform, so _create_shortcut() calls back into these slots
        # with the same convention they were built with either way.
        def query_interface(_this, _riid, out):
            ctypes.cast(out, ctypes.POINTER(ctypes.c_void_p))[0] = self.persist_file.value
            return 0

        def release(_this):
            self.released.append("shell_link")
            return 0

        def set_description(_this, text):
            self.description = text
            return 0

        def set_icon_location(_this, path, index):
            self.icon_location = (path, index)
            return 0

        def set_path(_this, path):
            self.path = path
            return 0

        return self._vtable(
            21,
            {
                0: ctypes.CFUNCTYPE(
                    self._HRESULT, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p
                )(query_interface),
                2: ctypes.CFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(release),
                7: ctypes.CFUNCTYPE(self._HRESULT, ctypes.c_void_p, ctypes.c_wchar_p)(
                    set_description
                ),
                17: ctypes.CFUNCTYPE(
                    self._HRESULT, ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int
                )(set_icon_location),
                20: ctypes.CFUNCTYPE(self._HRESULT, ctypes.c_void_p, ctypes.c_wchar_p)(set_path),
            },
        )

    def _build_persist_file_vtable(self):
        def release(_this):
            self.released.append("persist_file")
            return 0

        def save(_this, filename, _remember):
            if self._fail_save:
                return 1  # a failing HRESULT
            self.saved_to = filename
            return 0

        return self._vtable(
            9,
            {
                2: ctypes.CFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(release),
                6: ctypes.CFUNCTYPE(
                    self._HRESULT, ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int
                )(save),
            },
        )


def _patch_windows_com(monkeypatch, fake=None, *, cocreate_hresult=0):
    """Points `windows.ctypes.windll.ole32` at a fake `CoCreateInstance`
    that hands back `fake.shell_link` (a real vtable-shaped COM double --
    see `_FakeShellLinkCom`) -- the COM counterpart of
    `_patch_windows_hotkey_dll`. Also stands `ctypes.WINFUNCTYPE` in for
    `windows.ctypes.WINFUNCTYPE` off Windows, the same substitution
    test_capture.py's `_patch_windows_geometry_dll` already makes for the
    same reason: the real one only exists under `sys.platform == "win32"`,
    and `ctypes.CFUNCTYPE` builds the same kind of callable trampoline
    without ever crossing into real Win32 code.
    """

    def co_create_instance(_clsid, _outer, _clsctx, _iid, out):
        if cocreate_hresult != 0:
            return cocreate_hresult
        ctypes.cast(out, ctypes.POINTER(ctypes.c_void_p))[0] = fake.shell_link.value
        return 0

    ole32 = SimpleNamespace(
        CoInitialize=lambda _reserved: 0,
        CoUninitialize=lambda: None,
        CoCreateInstance=co_create_instance,
    )
    monkeypatch.setattr(windows.ctypes, "windll", SimpleNamespace(ole32=ole32), raising=False)
    monkeypatch.setattr(windows.ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE, raising=False)


class TestCreateShortcut:
    """AC: the Start Menu/Startup entries are real `.lnk` shortcuts, built
    through COM (`IShellLinkW`/`IPersistFile`) rather than a `.cmd` or bare
    copy of the executable -- `_create_shortcut` is the one place that
    happens.
    """

    def test_writes_the_target_description_and_icon_then_saves(self, monkeypatch):
        fake = _FakeShellLinkCom()
        _patch_windows_com(monkeypatch, fake)
        lnk_path = Path("C:/Users/x/snipux.lnk")
        target = Path("C:/Program Files/snipux/snipux.exe")
        icon_path = Path("C:/Users/x/AppData/Local/snipux/snipux.ico")

        result = windows._create_shortcut(
            lnk_path, target, icon_path=icon_path, description="snipux"
        )

        assert result is True
        # str(), not the forward-slash literal above: _create_shortcut hands
        # COM whatever str(Path(...)) gives it, which is backslash-separated
        # on the real Windows this suite sometimes runs directly on.
        assert fake.path == str(target)
        assert fake.description == "snipux"
        assert fake.icon_location == (str(icon_path), 0)
        assert fake.saved_to == str(lnk_path)
        # Both interfaces released, not just the one _create_shortcut asked
        # CoCreateInstance for -- a leaked COM reference on every --setup
        # run is exactly the kind of bug this fake exists to catch.
        assert fake.released == ["persist_file", "shell_link"]

    def test_no_description_or_icon_skips_those_calls(self, monkeypatch):
        fake = _FakeShellLinkCom()
        _patch_windows_com(monkeypatch, fake)
        lnk_path = Path("C:/x/snipux.lnk")

        windows._create_shortcut(lnk_path, Path("C:/x/snipux.exe"))

        assert fake.description is None
        assert fake.icon_location is None
        assert fake.saved_to == str(lnk_path)

    def test_a_cocreateinstance_failure_is_reported_as_false_not_raised(self, monkeypatch):
        _patch_windows_com(monkeypatch, cocreate_hresult=1)  # a failing HRESULT

        result = windows._create_shortcut(Path("C:/x/snipux.lnk"), Path("C:/x/snipux.exe"))

        assert result is False

    def test_a_save_failure_is_reported_as_false_not_raised(self, monkeypatch):
        fake = _FakeShellLinkCom(fail_save=True)
        _patch_windows_com(monkeypatch, fake)

        result = windows._create_shortcut(Path("C:/x/snipux.lnk"), Path("C:/x/snipux.exe"))

        assert result is False
        # Still released, even on failure -- a leak on the failure path
        # would be worse than one on the happy path, since a broken
        # shortcut is exactly when --setup is likely to be run again.
        assert fake.released == ["persist_file", "shell_link"]


class TestWriteAndRemoveIcon:
    """`_write_icon`/`_remove_icon` -- the Windows analogue of
    `setup_desktop.install_icons`/`remove_icons`, minus the multi-size
    hicolor layout: Windows gets one `.ico` file built from all the
    vendored sizes at once (`setup_desktop.render_ico`).
    """

    def test_writes_the_rendered_ico(self, monkeypatch, tmp_path):
        monkeypatch.setattr(setup_desktop, "render_ico", lambda: b"fake-ico-bytes")
        icon_path = tmp_path / "snipux" / "snipux.ico"

        result = windows._write_icon(icon_path)

        assert result is True
        assert icon_path.read_bytes() == b"fake-ico-bytes"

    def test_no_vendored_icon_is_a_note_not_a_failure(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setattr(setup_desktop, "render_ico", lambda: None)

        result = windows._write_icon(tmp_path / "snipux.ico")

        assert result is False
        assert "generic icon" in capsys.readouterr().out

    def test_removing_a_written_icon(self, tmp_path):
        icon_path = tmp_path / "snipux.ico"
        icon_path.write_bytes(b"x")

        assert windows._remove_icon(icon_path) is True
        assert not icon_path.exists()

    def test_removing_a_missing_icon_is_harmless(self, tmp_path, capsys):
        result = windows._remove_icon(tmp_path / "snipux.ico")

        assert result is True
        assert "nothing to remove" in capsys.readouterr().out


class TestPortableSelfInstall:
    """SNX-103: `_ensure_stable_copy()`/`_portable_exe_path()`/
    `_remove_stable_copy()` -- what relocates a portable `snipux.exe` to
    `%LOCALAPPDATA%\\snipux\\snipux.exe` before any shortcut is ever built,
    so the Start Menu/Startup entries `TestWindowsDesktopIntegration`
    covers below survive the original download being moved or deleted.

    `sys.frozen`/`sys.executable` are monkeypatched directly on the shared
    `sys` module (`windows.sys` *is* `sys`) -- the same pattern this file's
    `TestReattachConsole` already uses for `sys.platform` -- since that is
    the only thing PyInstaller actually sets to mark a frozen build; there
    is no fake to inject instead.
    """

    def _use_tmp_local_app_data(self, monkeypatch, tmp_path):
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))

    def _make_frozen(self, monkeypatch, exe_path: Path) -> None:
        exe_path.parent.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(windows.sys, "frozen", True, raising=False)
        monkeypatch.setattr(windows.sys, "executable", str(exe_path))

    def test_a_non_portable_build_is_a_noop(self, monkeypatch, tmp_path):
        # AC: "none of this happens for a pip or pipx install" -- neither
        # sets sys.frozen, so this must return before touching the
        # filesystem at all.
        self._use_tmp_local_app_data(monkeypatch, tmp_path)
        monkeypatch.setattr(windows.sys, "frozen", False, raising=False)

        assert windows._ensure_stable_copy() is None
        assert not (tmp_path / "Local" / "snipux" / "snipux.exe").exists()

    def test_first_run_copies_itself_to_the_stable_location_before_anything_else(
        self, monkeypatch, tmp_path
    ):
        self._use_tmp_local_app_data(monkeypatch, tmp_path)
        download = tmp_path / "Downloads" / "snipux.exe"
        self._make_frozen(monkeypatch, download)
        download.write_bytes(b"v1-bytes")

        result = windows._ensure_stable_copy()

        target = tmp_path / "Local" / "snipux" / "snipux.exe"
        assert result == target
        assert target.read_bytes() == b"v1-bytes"

    def test_running_the_installed_copy_does_not_copy_itself_again(self, monkeypatch, tmp_path):
        self._use_tmp_local_app_data(monkeypatch, tmp_path)
        target = tmp_path / "Local" / "snipux" / "snipux.exe"
        self._make_frozen(monkeypatch, target)
        target.write_bytes(b"already-installed")

        def _must_not_copy(*args, **kwargs):
            raise AssertionError("must not copy the installed exe onto itself")

        monkeypatch.setattr(windows.shutil, "copy2", _must_not_copy)

        result = windows._ensure_stable_copy()

        assert result == target
        assert target.read_bytes() == b"already-installed"

    def test_rerunning_the_same_download_does_not_recopy_or_nest_directories(
        self, monkeypatch, tmp_path
    ):
        self._use_tmp_local_app_data(monkeypatch, tmp_path)
        download = tmp_path / "Downloads" / "snipux.exe"
        self._make_frozen(monkeypatch, download)
        download.write_bytes(b"same-build-bytes")
        target_dir = tmp_path / "Local" / "snipux"
        target_dir.mkdir(parents=True)
        target = target_dir / "snipux.exe"
        target.write_bytes(b"same-build-bytes")  # same size -- already up to date

        def _must_not_copy(*args, **kwargs):
            raise AssertionError("must not recopy an unchanged download")

        monkeypatch.setattr(windows.shutil, "copy2", _must_not_copy)

        result = windows._ensure_stable_copy()

        assert result == target
        assert [p.name for p in target_dir.iterdir()] == ["snipux.exe"]

    def test_a_newer_version_replaces_the_older_install_rather_than_nesting(
        self, monkeypatch, tmp_path
    ):
        self._use_tmp_local_app_data(monkeypatch, tmp_path)
        download = tmp_path / "Downloads" / "snipux-new.exe"
        self._make_frozen(monkeypatch, download)
        download.write_bytes(b"v2-bytes-a-different-length")
        target_dir = tmp_path / "Local" / "snipux"
        target_dir.mkdir(parents=True)
        target = target_dir / "snipux.exe"
        target.write_bytes(b"v1")

        result = windows._ensure_stable_copy()

        assert result == target
        assert target.read_bytes() == b"v2-bytes-a-different-length"
        # Replaced in place, under the one fixed filename -- not left
        # alongside a second, older copy.
        assert [p.name for p in target_dir.iterdir()] == ["snipux.exe"]

    def test_deleting_the_original_download_afterwards_leaves_the_copy_working(
        self, monkeypatch, tmp_path
    ):
        self._use_tmp_local_app_data(monkeypatch, tmp_path)
        download = tmp_path / "Downloads" / "snipux.exe"
        self._make_frozen(monkeypatch, download)
        download.write_bytes(b"v1-bytes")
        target = windows._ensure_stable_copy()

        download.unlink()

        assert target.exists()
        assert target.read_bytes() == b"v1-bytes"

    def test_a_copy_failure_is_a_note_not_a_crash(self, monkeypatch, tmp_path, capsys):
        self._use_tmp_local_app_data(monkeypatch, tmp_path)
        download = tmp_path / "Downloads" / "snipux.exe"
        self._make_frozen(monkeypatch, download)
        download.write_bytes(b"v1-bytes")

        def _boom(*args, **kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(windows.shutil, "copy2", _boom)

        result = windows._ensure_stable_copy()

        assert result is None
        assert "could not copy" in capsys.readouterr().err

    def test_removing_the_relocated_copy(self, monkeypatch, tmp_path):
        self._use_tmp_local_app_data(monkeypatch, tmp_path)
        exe_path = tmp_path / "Local" / "snipux" / "snipux.exe"
        exe_path.parent.mkdir(parents=True)
        exe_path.write_bytes(b"x")

        assert windows._remove_stable_copy(exe_path) is True
        assert not exe_path.exists()

    def test_removing_a_copy_that_was_never_installed_is_harmless(self, tmp_path, capsys):
        exe_path = tmp_path / "snipux" / "snipux.exe"

        result = windows._remove_stable_copy(exe_path)

        assert result is True
        assert "nothing to remove" in capsys.readouterr().out

    def test_ensure_stable_install_forwards_to_ensure_stable_copy(self, monkeypatch):
        monkeypatch.setattr(windows, "_ensure_stable_copy", lambda: Path("C:/stable/snipux.exe"))

        assert windows.WindowsPlatform().ensure_stable_install() == Path("C:/stable/snipux.exe")


class TestWindowsDesktopIntegration:
    """AC: `snipux --setup`/`snipux --remove` on Windows create/remove a
    Start Menu entry and a Startup (login) entry, both showing the snipux
    icon, and running either command twice is harmless. `_create_shortcut`
    is faked here (see `TestCreateShortcut` above for its own real COM
    coverage) so these tests are about `install_desktop_integration`'s/
    `remove_desktop_integration`'s own orchestration: which paths, which
    order, which failures are fatal.
    """

    def _use_tmp_dirs(self, monkeypatch, tmp_path):
        monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
        monkeypatch.setattr(setup_desktop, "config_path", lambda config_dir=None: tmp_path / "config.json")
        monkeypatch.setattr(setup_desktop, "find_console_script", lambda: Path("C:/snipux/snipux.exe"))

    def test_writes_a_start_menu_and_a_startup_shortcut(self, monkeypatch, tmp_path):
        self._use_tmp_dirs(monkeypatch, tmp_path)
        created = []
        monkeypatch.setattr(
            windows,
            "_create_shortcut",
            lambda lnk, target, **kw: created.append((lnk, target)) or True,
        )

        exit_code = windows.WindowsPlatform().install_desktop_integration()

        assert exit_code == 0
        start_menu = tmp_path / "Roaming" / "Microsoft" / "Windows" / "Start Menu" / "Programs"
        [(lnk1, target1), (lnk2, target2)] = created
        assert lnk1 == start_menu / "snipux.lnk"
        assert lnk2 == start_menu / "Startup" / "snipux.lnk"
        assert target1 == target2 == Path("C:/snipux/snipux.exe")

    def test_says_nothing_about_the_shortcut_needing_a_restart(self, monkeypatch, tmp_path, capsys):
        """SNX-101: `AppController.run_first_launch_setup()` calls this from
        the already-resident process, and only after
        `install_hotkey_listener()` has already bound the shortcut in that
        same process -- so by the time this runs, the shortcut is already
        live, not "pending until next start". This function must not claim
        otherwise (or say anything about the shortcut's registration status
        at all -- that is `bind_shortcut()`'s own return value to report,
        surfaced through the tray/Settings, not a console line here).
        """
        self._use_tmp_dirs(monkeypatch, tmp_path)
        monkeypatch.setattr(windows, "_create_shortcut", lambda *a, **kw: True)

        windows.WindowsPlatform().install_desktop_integration()

        output = capsys.readouterr().out
        assert "restart" not in output.lower()
        assert "next time" not in output.lower()
        assert "registered" not in output.lower()

    def test_writes_the_icon_before_the_shortcuts_point_at_it(self, monkeypatch, tmp_path):
        self._use_tmp_dirs(monkeypatch, tmp_path)
        monkeypatch.setattr(setup_desktop, "render_ico", lambda: b"icon-bytes")
        icon_paths = []
        monkeypatch.setattr(
            windows,
            "_create_shortcut",
            lambda lnk, target, icon_path=None, **kw: icon_paths.append(icon_path) or True,
        )

        windows.WindowsPlatform().install_desktop_integration()

        expected_icon = tmp_path / "Local" / "snipux" / "snipux.ico"
        assert expected_icon.read_bytes() == b"icon-bytes"
        assert icon_paths == [expected_icon, expected_icon]

    def test_missing_console_script_is_fatal(self, monkeypatch, tmp_path):
        self._use_tmp_dirs(monkeypatch, tmp_path)
        monkeypatch.setattr(setup_desktop, "find_console_script", lambda: None)
        monkeypatch.setattr(windows, "_create_shortcut", lambda *a, **kw: True)

        exit_code = windows.WindowsPlatform().install_desktop_integration()

        assert exit_code == 1

    def test_a_shortcut_failing_to_write_does_not_stop_the_other(self, monkeypatch, tmp_path):
        self._use_tmp_dirs(monkeypatch, tmp_path)
        calls = []

        def flaky_create(lnk, target, **kw):
            calls.append(lnk)
            return "Startup" not in str(lnk)  # the Start Menu one fails

        monkeypatch.setattr(windows, "_create_shortcut", flaky_create)

        exit_code = windows.WindowsPlatform().install_desktop_integration()

        assert exit_code == 0
        assert len(calls) == 2  # both attempted despite the first failing

    def test_an_invalid_shortcut_is_rejected_before_writing_anything(self, monkeypatch, tmp_path):
        self._use_tmp_dirs(monkeypatch, tmp_path)
        monkeypatch.setattr(windows, "_create_shortcut", lambda *a, **kw: True)

        exit_code = windows.WindowsPlatform().install_desktop_integration(shortcut="S")

        assert exit_code == 1

    def test_a_valid_shortcut_is_remembered(self, monkeypatch, tmp_path):
        self._use_tmp_dirs(monkeypatch, tmp_path)
        monkeypatch.setattr(windows, "_create_shortcut", lambda *a, **kw: True)

        windows.WindowsPlatform().install_desktop_integration(shortcut="Control+Alt+X")

        assert setup_desktop.load_shortcut() == "Control+Alt+X"

    def test_running_setup_twice_is_harmless(self, monkeypatch, tmp_path):
        self._use_tmp_dirs(monkeypatch, tmp_path)
        monkeypatch.setattr(windows, "_create_shortcut", lambda *a, **kw: True)

        first = windows.WindowsPlatform().install_desktop_integration()
        second = windows.WindowsPlatform().install_desktop_integration()

        assert first == 0
        assert second == 0

    def test_a_portable_build_points_both_shortcuts_at_the_relocated_copy(
        self, monkeypatch, tmp_path
    ):
        # SNX-103: a frozen build's own exec_path (find_console_script()
        # returning wherever it was launched from) must be swapped out for
        # `_ensure_stable_copy()`'s stable path before either shortcut is
        # written -- not the original, possibly-in-Downloads location.
        self._use_tmp_dirs(monkeypatch, tmp_path)
        download = tmp_path / "Downloads" / "snipux.exe"
        download.parent.mkdir(parents=True)
        download.write_bytes(b"portable-build-bytes")
        monkeypatch.setattr(windows.sys, "frozen", True, raising=False)
        monkeypatch.setattr(windows.sys, "executable", str(download))
        monkeypatch.setattr(setup_desktop, "find_console_script", lambda: download.resolve())
        created = []
        monkeypatch.setattr(
            windows,
            "_create_shortcut",
            lambda lnk, target, **kw: created.append((lnk, target)) or True,
        )

        exit_code = windows.WindowsPlatform().install_desktop_integration()

        assert exit_code == 0
        stable_copy = tmp_path / "Local" / "snipux" / "snipux.exe"
        [(_, target1), (_, target2)] = created
        assert target1 == target2 == stable_copy
        assert stable_copy.read_bytes() == b"portable-build-bytes"

    def test_remove_deletes_both_shortcuts_and_the_icon(self, monkeypatch, tmp_path):
        self._use_tmp_dirs(monkeypatch, tmp_path)
        monkeypatch.setattr(windows, "_create_shortcut", lambda *a, **kw: True)
        windows.WindowsPlatform().install_desktop_integration()
        start_menu = tmp_path / "Roaming" / "Microsoft" / "Windows" / "Start Menu" / "Programs"
        icon_path = tmp_path / "Local" / "snipux" / "snipux.ico"
        assert icon_path.exists()  # install_desktop_integration() just wrote a real one

        exit_code = windows.WindowsPlatform().remove_desktop_integration()

        assert exit_code == 0
        assert not (start_menu / "snipux.lnk").exists()
        assert not (start_menu / "Startup" / "snipux.lnk").exists()
        assert not icon_path.exists()

    def test_remove_deletes_the_relocated_portable_copy(self, monkeypatch, tmp_path):
        self._use_tmp_dirs(monkeypatch, tmp_path)
        monkeypatch.setattr(windows, "_create_shortcut", lambda *a, **kw: True)
        exe_dir = tmp_path / "Local" / "snipux"
        exe_dir.mkdir(parents=True)
        exe_path = exe_dir / "snipux.exe"
        exe_path.write_bytes(b"installed-copy")

        exit_code = windows.WindowsPlatform().remove_desktop_integration()

        assert exit_code == 0
        assert not exe_path.exists()

    def test_remove_forgets_the_remembered_shortcut(self, monkeypatch, tmp_path):
        self._use_tmp_dirs(monkeypatch, tmp_path)
        setup_desktop.save_shortcut("Alt+Print")

        windows.WindowsPlatform().remove_desktop_integration()

        assert setup_desktop.load_shortcut() == setup_desktop.DEFAULT_SHORTCUT

    def test_running_remove_twice_is_harmless(self, monkeypatch, tmp_path):
        self._use_tmp_dirs(monkeypatch, tmp_path)

        first = windows.WindowsPlatform().remove_desktop_integration()
        second = windows.WindowsPlatform().remove_desktop_integration()

        assert first == 0
        assert second == 0


class TestAcceleratorToWin32:
    """`_accelerator_to_win32` -- the pure translation `bind_shortcut` builds
    on -- exercised directly so its edge cases (an unmappable key, a
    function key, a single digit) don't need a fake DLL to reach.
    """

    def test_a_single_letter_needs_no_lookup_table(self):
        assert windows._accelerator_to_win32("Control+Alt+S") == (
            0x0002 | 0x0001 | 0x4000,
            ord("S"),
        )

    def test_a_digit(self):
        assert windows._accelerator_to_win32("Control+1") == (0x0002 | 0x4000, ord("1"))

    def test_a_function_key(self):
        assert windows._accelerator_to_win32("Super+F12") == (0x0008 | 0x4000, 0x70 + 11)

    def test_a_named_key(self):
        assert windows._accelerator_to_win32("Alt+Print") == (0x0001 | 0x4000, 0x2C)

    def test_an_unmappable_key_is_none(self):
        assert windows._accelerator_to_win32("Control+NotAKey") is None

    def test_not_a_shortcut_at_all_is_none(self):
        assert windows._accelerator_to_win32("") is None


class TestReattachConsole:
    """SNX-100: `snipux.spec` now builds a windowed exe (no console of its
    own), so `reattach_console()` is what lets a terminal launch
    (`snipux --list-backends` etc.) still see its output, while a launch
    with no console anywhere in its parent chain (Explorer, a Start
    Menu/Startup shortcut) stays silent without crashing the first time
    something calls `print()`.

    `sys.stdout`/`sys.stderr` are restored after every test that lets
    `reattach_console()` actually reassign them -- this suite's own output
    capturing depends on them being the real thing again by the time this
    test returns control to pytest.
    """

    def test_a_non_windows_platform_never_touches_ctypes_windll(self, monkeypatch):
        monkeypatch.setattr(windows.sys, "platform", "linux")
        # windows.ctypes.windll is deliberately left unpatched: reaching
        # for it at all off Windows would itself raise, so this only
        # passes if reattach_console() returns before ever getting there.

        windows.reattach_console()  # must not raise

    def test_attaches_and_reopens_stdio_when_launched_from_a_terminal(self, monkeypatch):
        monkeypatch.setattr(windows.sys, "platform", "win32")
        monkeypatch.setattr(
            windows.ctypes,
            "windll",
            SimpleNamespace(kernel32=SimpleNamespace(AttachConsole=lambda pid: 1)),
            raising=False,
        )
        opened = []
        fake_streams = [SimpleNamespace(write=lambda s: None) for _ in range(2)]

        def fake_open(path, mode, **kwargs):
            opened.append(path)
            return fake_streams[len(opened) - 1]

        monkeypatch.setattr("builtins.open", fake_open)
        original_stdout, original_stderr = sys.stdout, sys.stderr
        try:
            windows.reattach_console()

            assert opened == ["CONOUT$", "CONOUT$"]
            assert sys.stdout is fake_streams[0]
            assert sys.stderr is fake_streams[1]
        finally:
            sys.stdout, sys.stderr = original_stdout, original_stderr

    def test_redirects_to_devnull_when_no_console_is_available(self, monkeypatch):
        # Explorer or a Start Menu/Startup shortcut: neither of those
        # parents has a console for AttachConsole to find, which is a
        # plain failure distinct from "this process already has one"
        # (ERROR_ACCESS_DENIED, covered below).
        monkeypatch.setattr(windows.sys, "platform", "win32")
        monkeypatch.setattr(
            windows.ctypes,
            "windll",
            SimpleNamespace(kernel32=SimpleNamespace(AttachConsole=lambda pid: 0)),
            raising=False,
        )
        monkeypatch.setattr(windows.ctypes, "GetLastError", lambda: 6, raising=False)
        opened = []
        fake_streams = [SimpleNamespace(write=lambda s: None) for _ in range(2)]

        def fake_open(path, mode, **kwargs):
            opened.append(path)
            return fake_streams[len(opened) - 1]

        monkeypatch.setattr("builtins.open", fake_open)
        original_stdout, original_stderr = sys.stdout, sys.stderr
        try:
            windows.reattach_console()

            assert opened == [windows.os.devnull, windows.os.devnull]
            assert sys.stdout is fake_streams[0]
            assert sys.stderr is fake_streams[1]
        finally:
            sys.stdout, sys.stderr = original_stdout, original_stderr

    def test_does_nothing_when_this_process_already_has_a_console(self, monkeypatch):
        # python -m snipux / the pip-installed console script: both start
        # with a real, already-working console inherited the ordinary way,
        # so AttachConsole fails with ERROR_ACCESS_DENIED rather than
        # finding nothing -- sys.stdout/stderr must be left alone.
        monkeypatch.setattr(windows.sys, "platform", "win32")
        monkeypatch.setattr(
            windows.ctypes,
            "windll",
            SimpleNamespace(kernel32=SimpleNamespace(AttachConsole=lambda pid: 0)),
            raising=False,
        )
        monkeypatch.setattr(windows.ctypes, "GetLastError", lambda: 5, raising=False)
        opened = []
        monkeypatch.setattr(
            "builtins.open", lambda path, mode, **kwargs: opened.append(path)
        )

        windows.reattach_console()

        assert opened == []


class TestHotkeyEventFilter:
    """AC: the hotkey works while another application has focus -- this is
    the mechanism that makes that true. `RegisterHotKey(None, ...)` posts
    `WM_HOTKEY` to this thread's message queue rather than to any window of
    ours, and this is the only hook (`QAbstractNativeEventFilter`) this
    process has into that queue.
    """

    @staticmethod
    def _msg_address(message: int) -> int:
        msg = windows._MSG(message=message)
        # Kept alive on the returned closure's behalf by the caller holding
        # a reference to `msg` -- see each test below.
        return msg, ctypes.addressof(msg)

    def test_is_available_only_on_windows(self, monkeypatch):
        monkeypatch.setattr(windows.sys, "platform", "win32")
        assert windows.HotkeyEventFilter.is_available() is True

        monkeypatch.setattr(windows.sys, "platform", "linux")
        assert windows.HotkeyEventFilter.is_available() is False

    def test_calls_on_triggered_for_a_wm_hotkey_message(self):
        calls = []
        filter_ = windows.HotkeyEventFilter(lambda: calls.append("triggered"))
        msg, address = self._msg_address(windows._WM_HOTKEY)

        result = filter_.nativeEventFilter(b"windows_generic_MSG", address)

        assert calls == ["triggered"]
        # Never claims to have handled the message -- see nativeEventFilter's
        # own docstring for why.
        assert result == (False, 0)

    def test_ignores_any_other_message(self):
        calls = []
        filter_ = windows.HotkeyEventFilter(lambda: calls.append("triggered"))
        msg, address = self._msg_address(0x0010)  # WM_CLOSE, not WM_HOTKEY

        filter_.nativeEventFilter(b"windows_generic_MSG", address)

        assert calls == []

    def test_ignores_a_non_windows_event_type(self):
        calls = []
        filter_ = windows.HotkeyEventFilter(lambda: calls.append("triggered"))
        msg, address = self._msg_address(windows._WM_HOTKEY)

        # A non-Windows-message native event (e.g. an X11/Wayland one on
        # some other platform's dispatcher) must never be read as a MSG
        # struct -- the address wouldn't even mean the same thing.
        filter_.nativeEventFilter(b"some_other_event_type", address)

        assert calls == []
