"""A bundled build must not hand its own libraries to the programs it runs.

The failure this guards was real: from the 1.1.4 AppImage every `hyprctl`
died on the bundle's older libstdc++, so the snip window's no-animation rule
was never installed and the desktop shrank and re-expanded on every snip.
"""

import os
import subprocess
import sys

import pytest

from snipux import hostenv

BUNDLE = "/tmp/.mount_Snipux123/usr/bin/_internal"


@pytest.fixture
def bundled(monkeypatch):
    monkeypatch.setenv("APPDIR", "/tmp/.mount_Snipux123")
    monkeypatch.delenv("LD_LIBRARY_PATH_ORIG", raising=False)
    yield
    hostenv.uninstall()


class TestHostEnv:
    def test_the_bundles_library_directory_is_removed(self, bundled):
        env = hostenv.host_env({"LD_LIBRARY_PATH": BUNDLE, "HOME": "/home/me"})

        assert "LD_LIBRARY_PATH" not in env
        assert env["HOME"] == "/home/me"

    def test_the_users_own_entries_survive(self, bundled):
        joined = os.pathsep.join(["/opt/cuda/lib", BUNDLE, "/opt/x"])
        env = hostenv.host_env({"LD_LIBRARY_PATH": joined})

        assert env["LD_LIBRARY_PATH"] == os.pathsep.join(["/opt/cuda/lib", "/opt/x"])

    def test_pyinstallers_saved_value_is_restored(self, bundled):
        env = hostenv.host_env(
            {"LD_LIBRARY_PATH": BUNDLE, "LD_LIBRARY_PATH_ORIG": "/opt/real"}
        )

        assert env["LD_LIBRARY_PATH"] == "/opt/real"
        assert "LD_LIBRARY_PATH_ORIG" not in env

    def test_an_empty_saved_value_means_it_was_unset(self, bundled):
        env = hostenv.host_env({"LD_LIBRARY_PATH": BUNDLE, "LD_LIBRARY_PATH_ORIG": ""})

        assert "LD_LIBRARY_PATH" not in env

    def test_a_normal_install_is_left_exactly_alone(self, monkeypatch):
        monkeypatch.delenv("APPDIR", raising=False)
        before = {"LD_LIBRARY_PATH": "/opt/cuda/lib", "PATH": "/usr/bin"}

        assert hostenv.host_env(before) == before

    def test_a_directory_that_merely_shares_a_prefix_is_not_the_bundle(self, bundled):
        env = hostenv.host_env({"LD_LIBRARY_PATH": "/tmp/.mount_Snipux123-other/lib"})

        assert env["LD_LIBRARY_PATH"] == "/tmp/.mount_Snipux123-other/lib"


class TestInstall:
    def test_children_are_started_without_the_bundles_libraries(self, bundled, monkeypatch):
        monkeypatch.setenv("LD_LIBRARY_PATH", BUNDLE)
        assert hostenv.install() is True

        out = subprocess.run(
            [sys.executable, "-c", "import os; print(os.environ.get('LD_LIBRARY_PATH', 'unset'))"],
            capture_output=True,
            text=True,
        ).stdout.strip()

        assert out == "unset"
        # The process itself keeps it: it is still loading plugins from there.
        assert os.environ["LD_LIBRARY_PATH"] == BUNDLE

    def test_a_caller_who_passes_their_own_env_is_not_overridden(self, bundled, monkeypatch):
        monkeypatch.setenv("LD_LIBRARY_PATH", BUNDLE)
        hostenv.install()

        out = subprocess.run(
            [sys.executable, "-c", "import os; print(os.environ.get('LD_LIBRARY_PATH'))"],
            capture_output=True,
            text=True,
            env={**os.environ, "LD_LIBRARY_PATH": "/deliberate"},
        ).stdout.strip()

        assert out == "/deliberate"

    def test_nothing_is_patched_outside_a_bundle(self, monkeypatch):
        monkeypatch.delenv("APPDIR", raising=False)
        monkeypatch.setenv("LD_LIBRARY_PATH", "/opt/cuda/lib")
        before = subprocess.Popen.__init__

        assert hostenv.install() is False
        assert subprocess.Popen.__init__ is before
