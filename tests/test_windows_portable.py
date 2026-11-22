"""The portable exe installing itself as a folder build
(`snipux/platform/windows_portable.py`).

Nothing here runs a real exe or a real build. The single-file build is
faked the only way it can be told apart at runtime -- `sys.frozen`, and a
`sys._MEIPASS` folder holding the folder build's exe -- and everything it
would start goes through a recorded `subprocess`.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from snipux.platform import windows_portable

REPO_ROOT = Path(__file__).resolve().parent.parent


class _SingleFile:
    """A faked single-file build: the folder its bootloader unpacked into,
    where it would install itself, and what it started."""

    def __init__(self, monkeypatch, tmp_path: Path):
        self.bundle = tmp_path / "Temp" / "_MEI12345"
        self.folder_exe = self.bundle / windows_portable.FOLDER_EXE
        self.folder_exe.parent.mkdir(parents=True)
        self.folder_exe.write_bytes(b"folder-exe-of-this-build")
        (self.bundle / "python3.dll").write_bytes(b"python")
        (self.bundle / "PyQt6").mkdir()
        (self.bundle / "PyQt6" / "Qt6Core.dll").write_bytes(b"qt")
        self.target = tmp_path / "Local" / "snipux" / "snipux.exe"
        self.root = self.target.parent
        self.libraries = self.root / "_internal"

        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.setattr(sys, "_MEIPASS", str(self.bundle), raising=False)
        monkeypatch.setattr(sys, "executable", str(tmp_path / "Downloads" / "snipux.exe"))

        self.started: list[tuple[str, list[str], dict]] = []
        self.exit_code = 0
        self.probe_passes = True
        monkeypatch.setattr(windows_portable.subprocess, "run", self._run)
        monkeypatch.setattr(windows_portable.subprocess, "Popen", self._popen)
        monkeypatch.setattr(windows_portable, "_starts", self._probe)
        self.probed: list[Path] = []

    def _run(self, command, **kwargs):
        self.started.append(("run", command, kwargs))
        return SimpleNamespace(returncode=self.exit_code)

    def _popen(self, command, **kwargs):
        self.started.append(("popen", command, kwargs))

    def _probe(self, exe: Path) -> bool:
        self.probed.append(exe)
        # The staged copy has to be whole by the time it is tried.
        assert (exe.parent / "_internal" / "python3.dll").is_file()
        return self.probe_passes

    def install_an_older_build(self) -> None:
        self.libraries.mkdir(parents=True)
        self.target.write_bytes(b"older-folder-exe")
        (self.libraries / "dropped-in-this-version.dll").write_bytes(b"old")

    def hand_off(self, *arguments: str, **kwargs):
        return windows_portable.hand_off(self.target, list(arguments), **kwargs)

    def leftovers(self) -> set[str]:
        return {entry.name for entry in self.root.iterdir()} - {"snipux.exe", "_internal"}


@pytest.fixture
def single_file(monkeypatch, tmp_path):
    return _SingleFile(monkeypatch, tmp_path)


class TestWhichBuildsHandOff:
    def test_a_build_that_is_not_frozen_carries_on_itself(self, monkeypatch, tmp_path):
        monkeypatch.setattr(sys, "frozen", False, raising=False)
        target = tmp_path / "Local" / "snipux" / "snipux.exe"

        assert windows_portable.hand_off(target, ["--snip"]) is None
        assert not target.parent.exists()

    def test_the_folder_build_itself_carries_on_itself(self, monkeypatch, tmp_path):
        # Frozen, but with no folder exe inside it: the installer's build,
        # and the one this installs.
        bundle = tmp_path / "snipux" / "_internal"
        bundle.mkdir(parents=True)
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.setattr(sys, "_MEIPASS", str(bundle), raising=False)
        target = tmp_path / "Local" / "snipux" / "snipux.exe"

        assert windows_portable.hand_off(target, []) is None
        assert not target.parent.exists()

    def test_removal_is_never_handed_off(self, single_file):
        # It deletes the folder build, which a process running from that
        # folder cannot do.
        assert single_file.hand_off("--remove") is None
        assert not single_file.root.exists()
        assert single_file.started == []

    def test_the_installers_install_is_left_alone(self, single_file):
        single_file.install_an_older_build()
        (single_file.root / "unins000.exe").write_bytes(b"uninstaller")

        assert single_file.hand_off() is None

        assert single_file.target.read_bytes() == b"older-folder-exe"
        assert (single_file.libraries / "dropped-in-this-version.dll").is_file()
        assert single_file.started == []


    def test_the_single_file_copy_running_from_the_install_folder_carries_on(
        self, single_file, monkeypatch, capsys
    ):
        # The fallback: a machine where the folder build could not be
        # installed runs the single file from there, as before.
        single_file.root.mkdir(parents=True)
        single_file.target.write_bytes(b"the-whole-single-file-exe")
        monkeypatch.setattr(sys, "executable", str(single_file.target))

        assert single_file.hand_off() is None

        assert single_file.target.read_bytes() == b"the-whole-single-file-exe"
        assert capsys.readouterr().err == ""


class TestFirstRun:
    def test_first_run_installs_a_folder_build(self, single_file):
        single_file.hand_off("--list-backends")

        assert single_file.target.read_bytes() == b"folder-exe-of-this-build"
        assert (single_file.libraries / "python3.dll").read_bytes() == b"python"
        assert (single_file.libraries / "PyQt6" / "Qt6Core.dll").read_bytes() == b"qt"

    def test_the_carried_exe_is_not_copied_into_the_libraries_as_well(self, single_file):
        single_file.hand_off()

        assert not (single_file.libraries / "_folder_build").exists()

    def test_a_command_runs_from_the_folder_build_and_its_exit_code_comes_back(
        self, single_file
    ):
        single_file.exit_code = 3

        assert single_file.hand_off("--list-backends") == 3

        how, command, _kwargs = single_file.started[-1]
        assert how == "run"
        assert command == [str(single_file.target), "--list-backends"]

    def test_a_bare_launch_starts_the_tray_app_on_its_own_and_ends(self, single_file):
        assert single_file.hand_off() == 0

        how, command, kwargs = single_file.started[-1]
        assert how == "popen"   # not waited for: it runs until Quit
        assert command == [str(single_file.target)]
        assert kwargs["cwd"] == str(single_file.root)

    def test_the_folder_build_is_told_it_is_not_this_bundles_child(self, single_file):
        single_file.hand_off("--snip")

        _how, _command, kwargs = single_file.started[-1]
        assert kwargs["env"]["PYINSTALLER_RESET_ENVIRONMENT"] == "1"

    def test_the_staged_copy_is_tried_before_it_is_installed(self, single_file):
        single_file.hand_off()

        assert single_file.probed == [single_file.root / ".staging" / "snipux.exe"]

    def test_nothing_but_the_build_is_left_in_the_folder(self, single_file):
        single_file.hand_off()

        assert single_file.leftovers() == set()

    def test_the_caller_is_told_once_the_install_is_settled_and_before_the_start(
        self, single_file
    ):
        seen = []

        def before_start():
            seen.append((single_file.target.is_file(), list(single_file.started)))

        single_file.hand_off("--snip", before_start=before_start)

        assert seen == [(True, [])]


class TestLaterRuns:
    def test_the_same_build_run_again_installs_nothing(self, single_file, monkeypatch):
        single_file.hand_off()

        def must_not_copy(*args, **kwargs):
            raise AssertionError("an install that is already this build was copied again")

        monkeypatch.setattr(windows_portable.shutil, "copytree", must_not_copy)

        assert single_file.hand_off("--snip") == 0
        assert single_file.started[-1][1] == [str(single_file.target), "--snip"]

    def test_a_newer_build_replaces_the_older_one_and_its_libraries(self, single_file):
        single_file.install_an_older_build()

        single_file.hand_off()

        assert single_file.target.read_bytes() == b"folder-exe-of-this-build"
        assert (single_file.libraries / "python3.dll").is_file()
        # Replaced, not merged: a library the old version loaded and this
        # one dropped must not still be there to load.
        assert not (single_file.libraries / "dropped-in-this-version.dll").exists()
        assert single_file.leftovers() == set()

    def test_an_old_single_file_copy_is_replaced_by_a_folder_build(self, single_file):
        # What every portable release before this one left there.
        single_file.root.mkdir(parents=True)
        single_file.target.write_bytes(b"the-whole-single-file-exe")

        single_file.hand_off()

        assert single_file.target.read_bytes() == b"folder-exe-of-this-build"
        assert (single_file.libraries / "python3.dll").is_file()

    def test_an_exe_without_its_libraries_is_installed_again(self, single_file):
        single_file.root.mkdir(parents=True)
        single_file.target.write_bytes(b"folder-exe-of-this-build")

        single_file.hand_off()

        assert (single_file.libraries / "python3.dll").is_file()

    def test_what_an_interrupted_install_left_is_cleared(self, single_file):
        for name in (".staging", ".replaced"):
            (single_file.root / name / "_internal").mkdir(parents=True)
            (single_file.root / name / "_internal" / "half.dll").write_bytes(b"x")

        single_file.hand_off()

        assert single_file.leftovers() == set()
        assert not (single_file.libraries / "half.dll").exists()


class TestFallingBack:
    """Each of these ends with None -- the launch carries on in the
    single-file process, as every launch did before -- a line in
    snipux.log, and whatever was installed before still in place.
    """

    def _assert_the_older_build_is_intact(self, single_file):
        assert single_file.target.read_bytes() == b"older-folder-exe"
        assert (single_file.libraries / "dropped-in-this-version.dll").read_bytes() == b"old"
        assert single_file.leftovers() == set()

    def test_a_copy_that_does_not_start_is_never_installed(self, single_file, capsys):
        single_file.install_an_older_build()
        single_file.probe_passes = False

        assert single_file.hand_off() is None

        self._assert_the_older_build_is_intact(single_file)
        assert single_file.started == []
        assert "did not start" in capsys.readouterr().err

    def test_a_running_install_is_not_replaced(self, single_file, monkeypatch, capsys):
        single_file.install_an_older_build()
        monkeypatch.setattr(windows_portable, "_in_use", lambda exe: True)

        def must_not_copy(*args, **kwargs):
            raise AssertionError("copied a build that could not have been moved into place")

        monkeypatch.setattr(windows_portable.shutil, "copytree", must_not_copy)

        assert single_file.hand_off() is None

        self._assert_the_older_build_is_intact(single_file)
        assert "Quit Snipux" in capsys.readouterr().err

    def test_a_folder_that_cannot_be_written_is_a_note_not_a_crash(
        self, single_file, monkeypatch, capsys
    ):
        def read_only(*args, **kwargs):
            raise PermissionError("access is denied")

        monkeypatch.setattr(windows_portable.shutil, "copytree", read_only)

        assert single_file.hand_off() is None

        assert not single_file.target.exists()
        assert "could not install Snipux" in capsys.readouterr().err

    def test_a_failure_is_written_to_the_log(self, single_file, monkeypatch):
        from snipux import log

        monkeypatch.setattr(
            windows_portable.shutil,
            "copytree",
            lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk full")),
        )

        single_file.hand_off()

        assert "disk full" in log.notes_path().read_text(encoding="utf-8")

    def test_a_move_into_place_that_fails_puts_the_older_build_back(
        self, single_file, monkeypatch, capsys
    ):
        single_file.install_an_older_build()

        def refuse(source, destination):
            raise PermissionError("access is denied")

        # The last step: by now the old build is out and the new libraries in.
        monkeypatch.setattr(windows_portable.os, "replace", refuse)

        assert single_file.hand_off() is None

        self._assert_the_older_build_is_intact(single_file)
        assert "could not install Snipux" in capsys.readouterr().err

    def test_libraries_that_cannot_be_moved_out_leave_the_exe_where_it_was(
        self, single_file, monkeypatch
    ):
        single_file.install_an_older_build()
        real_rename = windows_portable.os.rename

        def libraries_are_loaded(source, destination):
            if Path(source) == single_file.libraries:
                raise PermissionError("in use")
            real_rename(source, destination)

        monkeypatch.setattr(windows_portable.os, "rename", libraries_are_loaded)

        assert single_file.hand_off() is None

        self._assert_the_older_build_is_intact(single_file)

    def test_a_folder_build_that_cannot_be_started_carries_on_here(
        self, single_file, monkeypatch, capsys
    ):
        def blocked(command, **kwargs):
            raise OSError("blocked by an application control policy")

        monkeypatch.setattr(windows_portable.subprocess, "run", blocked)

        assert single_file.hand_off("--snip") is None
        assert "could not start" in capsys.readouterr().err


class TestTryingTheStagedCopy:
    def _run_returning(self, monkeypatch, outcome):
        commands = []

        def run(command, **kwargs):
            commands.append((command, kwargs))
            if isinstance(outcome, BaseException):
                raise outcome
            return SimpleNamespace(returncode=outcome)

        monkeypatch.setattr(windows_portable.subprocess, "run", run)
        return commands

    def test_it_runs_help_with_nowhere_to_print(self, monkeypatch, tmp_path):
        commands = self._run_returning(monkeypatch, 0)
        exe = tmp_path / "snipux.exe"

        assert windows_portable._starts(exe) is True

        command, kwargs = commands[0]
        assert command == [str(exe), "--help"]
        assert kwargs["stdout"] == subprocess.DEVNULL
        assert kwargs["env"]["PYINSTALLER_RESET_ENVIRONMENT"] == "1"

    def test_a_copy_that_exits_with_an_error_did_not_start(self, monkeypatch, tmp_path):
        self._run_returning(monkeypatch, 1)

        assert windows_portable._starts(tmp_path / "snipux.exe") is False

    def test_a_copy_windows_refuses_to_run_did_not_start(self, monkeypatch, tmp_path):
        self._run_returning(monkeypatch, OSError("blocked"))

        assert windows_portable._starts(tmp_path / "snipux.exe") is False

    def test_a_copy_that_hangs_did_not_start(self, monkeypatch, tmp_path):
        self._run_returning(monkeypatch, subprocess.TimeoutExpired("snipux.exe", 60))

        assert windows_portable._starts(tmp_path / "snipux.exe") is False


class TestInUse:
    def test_a_file_nothing_holds_is_not_in_use(self, tmp_path):
        exe = tmp_path / "snipux.exe"
        exe.write_bytes(b"exe")

        assert windows_portable._in_use(exe) is False
        assert exe.read_bytes() == b"exe"

    def test_a_missing_file_is_not_in_use(self, tmp_path):
        assert windows_portable._in_use(tmp_path / "snipux.exe") is False


def test_the_spec_bundles_the_folder_exe_where_this_module_looks():
    # Two spellings of one path, in a .spec that cannot import this module
    # without importing Qt. If they drift the portable exe just never
    # installs itself, and nothing else would say so.
    spec = (REPO_ROOT / "packaging" / "windows" / "snipux.spec").read_text(encoding="utf-8")

    assert f'"{windows_portable.FOLDER_EXE}"' in spec
