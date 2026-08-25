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
"""

import ctypes
import importlib
import sys
from types import SimpleNamespace

import pytest

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


class TestStubPlatforms:
    """AC: macOS's implementation exists and raises a clear error naming the
    platform and the operation that isn't implemented yet. Windows'
    `bind_shortcut`/`unbind_shortcut` are real now (SNX-91) -- see
    `TestWindowsPlatform` below -- so it keeps only the operations still
    unimplemented there.
    """

    DARWIN_OPERATIONS = (
        "install_desktop_integration",
        "remove_desktop_integration",
        "bind_shortcut",
        "unbind_shortcut",
        "default_save_folder",
    )

    WINDOWS_UNIMPLEMENTED_OPERATIONS = (
        "install_desktop_integration",
        "remove_desktop_integration",
        "default_save_folder",
    )

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

        assert "No snipux shortcut is currently registered" in result
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
