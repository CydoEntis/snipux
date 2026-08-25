"""SNX-85: the platform seam -- installing/removing desktop integration,
binding/unbinding the global shortcut, and reporting where a saved image
should go, gathered behind one interface (`snipux.platform.Platform`) and
selected by `sys.platform` at import time.

`LinuxPlatform` is a thin adapter onto `snipux.setup_desktop`, which already
has its own, much larger test suite (test_setup_desktop.py) covering the
actual `.desktop`/gsettings/XDG behaviour -- these tests only prove the
adapter forwards to the right function with the right arguments and hands
back what it returned, not that behaviour again.
"""

import importlib
import sys

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
    """AC: the Windows and macOS implementations exist and raise a clear
    error naming the platform and the operation that isn't implemented yet.
    """

    OPERATIONS = (
        "install_desktop_integration",
        "remove_desktop_integration",
        "bind_shortcut",
        "unbind_shortcut",
        "default_save_folder",
    )

    @pytest.mark.parametrize(
        "cls,platform_name",
        [(windows.WindowsPlatform, "Windows"), (darwin.DarwinPlatform, "macOS")],
    )
    def test_every_operation_raises_naming_the_platform_and_the_operation(
        self, cls, platform_name
    ):
        stub = cls()

        for operation in self.OPERATIONS:
            with pytest.raises(UnimplementedPlatformError) as excinfo:
                getattr(stub, operation)()
            message = str(excinfo.value)
            assert platform_name in message
            assert operation in message

    def test_stubs_still_satisfy_the_platform_interface(self):
        # Neither stub can be constructed at all unless every abstract
        # method of Platform is implemented -- proves the stub is a real,
        # complete implementation of the seam rather than a partial one
        # that just happens not to be exercised yet.
        assert isinstance(windows.WindowsPlatform(), Platform)
        assert isinstance(darwin.DarwinPlatform(), Platform)
