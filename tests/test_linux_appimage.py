"""An AppImage replacing itself (`snipux/platform/linux_appimage.py`).

Nothing here is a real AppImage, and nothing is started: the "AppImage" is
a few bytes in `tmp_path`, `$APPIMAGE` is set by hand the way AppRun sets
it, and the launcher is a recorder. Failures of the disk are raised from
the call that would meet them, not produced by filling or locking a real
folder, so the answers are the same on both CI runners and for root.
"""

from __future__ import annotations

import errno
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import skip_on_windows
from snipux.platform import UpdateError, linux_appimage

OLD = b"the AppImage that is running"
NEW = b"the AppImage the release published"


@pytest.fixture
def install_dir(tmp_path, monkeypatch):
    """A folder holding a running AppImage, and a verified download
    waiting in a folder of its own."""
    folder = tmp_path / "apps"
    folder.mkdir()
    appimage = folder / "Snipux.AppImage"
    appimage.write_bytes(OLD)
    downloads = tmp_path / "snipux-update-abc"
    downloads.mkdir()
    downloaded = downloads / "Snipux-2.0.0-x86_64.AppImage"
    downloaded.write_bytes(NEW)
    monkeypatch.setenv("APPIMAGE", str(appimage))
    return appimage, downloaded


def _names(folder: Path) -> list[str]:
    return sorted(path.name for path in folder.iterdir())


class TestWhichFileIsRunning:
    def test_a_snipux_not_started_from_an_appimage_has_none(self, monkeypatch):
        monkeypatch.delenv("APPIMAGE", raising=False)

        assert linux_appimage.running_appimage() is None

    def test_an_appimage_that_is_no_longer_there_is_not_one(self, monkeypatch, tmp_path):
        # Deleted or moved while running: there is no path to put a new
        # one at that anything still points to.
        monkeypatch.setenv("APPIMAGE", str(tmp_path / "gone.AppImage"))

        assert linux_appimage.running_appimage() is None

    def test_it_is_the_file_apprun_names(self, install_dir):
        appimage, _downloaded = install_dir

        assert linux_appimage.running_appimage() == appimage.resolve()

    @skip_on_windows("making a symlink needs a privilege Windows does not give by default")
    def test_a_symlink_to_it_is_followed_to_the_file(self, install_dir, monkeypatch):
        # /usr/bin/snipux -> /opt/snipux/Snipux.AppImage: the file is what
        # gets replaced, and the link goes on pointing at it.
        appimage, _downloaded = install_dir
        link = appimage.parent / "snipux"
        link.symlink_to(appimage)
        monkeypatch.setenv("APPIMAGE", str(link))

        assert linux_appimage.running_appimage() == appimage.resolve()

    def test_a_folder_it_cannot_write_to_cannot_be_updated_in(self, install_dir, monkeypatch):
        appimage, _downloaded = install_dir
        assert linux_appimage.can_replace(appimage)

        monkeypatch.setattr(linux_appimage.os, "access", lambda path, mode: False)

        assert not linux_appimage.can_replace(appimage)

    def test_the_asset_is_named_as_the_release_names_it(self, monkeypatch):
        monkeypatch.setattr(linux_appimage, "_machine", lambda: "x86_64")

        assert linux_appimage.asset_name("1.5.0") == "Snipux-1.5.0-x86_64.AppImage"


class TestInstalling:
    def test_the_new_file_takes_the_old_ones_place(self, install_dir):
        appimage, downloaded = install_dir

        backup = linux_appimage.install(downloaded, appimage, "1.4.0")

        assert appimage.read_bytes() == NEW
        assert backup == appimage.parent / "Snipux.AppImage.1.4.0.bak"
        assert backup.read_bytes() == OLD
        # Nothing staged left behind, and the download is not kept: it has
        # been copied, and it is 80 MB.
        assert _names(appimage.parent) == ["Snipux.AppImage", "Snipux.AppImage.1.4.0.bak"]
        assert not downloaded.exists()

    @skip_on_windows("Windows has no execute bit to set")
    def test_it_can_be_run(self, install_dir):
        appimage, downloaded = install_dir
        os.chmod(downloaded, 0o600)   # as `updates.download` leaves it

        linux_appimage.install(downloaded, appimage, "1.4.0")

        assert stat.S_IMODE(appimage.stat().st_mode) == 0o755

    @skip_on_windows("Windows refuses to replace a file that is open; an AppImage never runs there")
    def test_the_running_file_is_replaced_not_written_into(self, install_dir):
        # The rule the module exists for. AppRun holds the old file open
        # and the process is executing out of it: writing into it would
        # change those pages. Replacing leaves the open file exactly as it
        # was, under the backup's name.
        appimage, downloaded = install_dir
        old_inode = appimage.stat().st_ino

        with open(appimage, "rb") as held_open:
            backup = linux_appimage.install(downloaded, appimage, "1.4.0")
            assert held_open.read() == OLD

        assert appimage.stat().st_ino != old_inode
        assert backup.stat().st_ino == old_inode

    def test_it_is_staged_in_the_same_folder(self, install_dir, monkeypatch):
        # A rename across filesystems is a copy, and a copy is not atomic.
        appimage, downloaded = install_dir
        renames = []
        real_replace = os.replace

        def replace(source, target):
            renames.append((Path(source), Path(target)))
            real_replace(source, target)

        monkeypatch.setattr(linux_appimage.os, "replace", replace)

        linux_appimage.install(downloaded, appimage, "1.4.0")

        staged, target = renames[-1]
        assert target == appimage
        assert staged.parent == appimage.parent

    def test_only_the_newest_backup_is_kept(self, install_dir):
        appimage, downloaded = install_dir
        folder = appimage.parent
        (folder / "Snipux.AppImage.1.2.0.bak").write_bytes(b"older")
        (folder / "Snipux.AppImage.1.3.0.bak").write_bytes(b"old")
        (folder / "notes.bak").write_bytes(b"someone else's")
        (folder / "Other.AppImage.1.0.0.bak").write_bytes(b"another app's")

        linux_appimage.install(downloaded, appimage, "1.4.0")

        assert _names(folder) == [
            "Other.AppImage.1.0.0.bak",
            "Snipux.AppImage",
            "Snipux.AppImage.1.4.0.bak",
            "notes.bak",
        ]

    def test_a_backup_from_the_same_version_is_replaced(self, install_dir):
        appimage, downloaded = install_dir
        stale = appimage.parent / "Snipux.AppImage.1.4.0.bak"
        stale.write_bytes(b"an earlier 1.4.0")

        linux_appimage.install(downloaded, appimage, "1.4.0")

        assert stale.read_bytes() == OLD

    def test_a_filesystem_without_hard_links_gets_a_copy(self, install_dir, monkeypatch):
        appimage, downloaded = install_dir

        def no_links(source, target):
            raise OSError(errno.EPERM, "Operation not permitted")

        monkeypatch.setattr(linux_appimage.os, "link", no_links)

        backup = linux_appimage.install(downloaded, appimage, "1.4.0")

        assert backup.read_bytes() == OLD
        assert appimage.read_bytes() == NEW
        assert _names(appimage.parent) == ["Snipux.AppImage", "Snipux.AppImage.1.4.0.bak"]


class TestWhenItCannot:
    """Every failure leaves the AppImage that was there, and says what to
    do about it."""

    def _older_backup(self, appimage: Path) -> Path:
        older = appimage.parent / "Snipux.AppImage.1.3.0.bak"
        older.write_bytes(b"old")
        return older

    def test_a_full_disk_changes_nothing(self, install_dir, monkeypatch):
        appimage, downloaded = install_dir
        older = self._older_backup(appimage)

        def full(source, target):
            target.write(b"half of")
            raise OSError(errno.ENOSPC, "No space left on device")

        monkeypatch.setattr(linux_appimage.shutil, "copyfileobj", full)

        with pytest.raises(UpdateError) as failure:
            linux_appimage.install(downloaded, appimage, "1.4.0")

        assert "not enough free space" in str(failure.value)
        assert "Free some space" in str(failure.value)
        assert appimage.read_bytes() == OLD
        # No half-written file, and the way back that was there still is.
        assert _names(appimage.parent) == ["Snipux.AppImage", older.name]

    def test_a_folder_that_refuses_changes_nothing(self, install_dir, monkeypatch):
        appimage, downloaded = install_dir

        def refuse(source, target):
            raise PermissionError(errno.EACCES, "Permission denied")

        monkeypatch.setattr(linux_appimage.os, "replace", refuse)

        with pytest.raises(UpdateError) as failure:
            linux_appimage.install(downloaded, appimage, "1.4.0")

        said = str(failure.value)
        assert "cannot write to" in said and str(appimage.parent) in said
        assert "Releases page" in said
        assert appimage.read_bytes() == OLD
        assert not (appimage.parent / ".Snipux.AppImage.new").exists()

    def test_a_read_only_filesystem_says_the_same(self, install_dir, monkeypatch):
        appimage, downloaded = install_dir

        def read_only(*args, **kwargs):
            raise OSError(errno.EROFS, "Read-only file system")

        monkeypatch.setattr(linux_appimage.shutil, "copyfileobj", read_only)

        with pytest.raises(UpdateError) as failure:
            linux_appimage.install(downloaded, appimage, "1.4.0")

        assert "cannot write to" in str(failure.value)
        assert appimage.read_bytes() == OLD

    def test_no_backup_means_no_update(self, install_dir, monkeypatch):
        # The old version is the way back if the new one will not start;
        # an update that cannot keep it does not go ahead.
        appimage, downloaded = install_dir

        def fail(*args, **kwargs):
            raise OSError(errno.ENOSPC, "No space left on device")

        monkeypatch.setattr(linux_appimage.os, "link", fail)
        monkeypatch.setattr(linux_appimage.shutil, "copy2", fail)

        with pytest.raises(UpdateError):
            linux_appimage.install(downloaded, appimage, "1.4.0")

        assert appimage.read_bytes() == OLD
        assert _names(appimage.parent) == ["Snipux.AppImage"]

    def test_any_other_failure_names_itself(self, install_dir, monkeypatch):
        appimage, downloaded = install_dir

        def odd(source, target):
            raise OSError(errno.EIO, "Input/output error")

        monkeypatch.setattr(linux_appimage.os, "replace", odd)

        with pytest.raises(UpdateError) as failure:
            linux_appimage.install(downloaded, appimage, "1.4.0")

        assert "Input/output error" in str(failure.value)
        assert "nothing was changed" in str(failure.value)
        assert appimage.read_bytes() == OLD


class TestStartingTheNewOne:
    def _launch(self, appimage):
        started = []

        def launcher(command, **kwargs):
            started.append((command, kwargs))

        assert linux_appimage.relaunch(appimage, launcher) is True
        (command, kwargs), = started
        return command, kwargs

    def test_it_starts_the_file_at_the_same_path(self, install_dir):
        appimage, _downloaded = install_dir

        command, _kwargs = self._launch(appimage)

        # Bare: a launch with no arguments is the one that stays resident.
        assert command == [str(appimage)]

    def test_it_is_a_process_of_its_own(self, install_dir):
        # The old snipux exits a moment later. A child in its session and
        # holding its pipes would be taken down with it.
        appimage, _downloaded = install_dir

        _command, kwargs = self._launch(appimage)

        assert kwargs["start_new_session"] is True
        assert kwargs["stdin"] == subprocess.DEVNULL
        assert kwargs["stdout"] == subprocess.DEVNULL
        assert kwargs["stderr"] == subprocess.DEVNULL
        assert kwargs["cwd"] == str(Path.home())

    def test_it_does_not_inherit_the_old_mount(self, install_dir, monkeypatch):
        appimage, _downloaded = install_dir
        monkeypatch.setenv("APPDIR", "/tmp/.mount_SnipuxOLD")
        monkeypatch.setenv("ARGV0", "Snipux.AppImage")
        monkeypatch.setenv("OWD", "/home/someone")
        monkeypatch.setenv("LD_LIBRARY_PATH", "/tmp/.mount_SnipuxOLD/usr/bin/_internal")
        monkeypatch.setenv("LD_LIBRARY_PATH_ORIG", "/opt/host/lib")

        _command, kwargs = self._launch(appimage)

        env = kwargs["env"]
        for name in ("APPIMAGE", "APPDIR", "ARGV0", "OWD"):
            assert name not in env
        assert env["LD_LIBRARY_PATH"] == "/opt/host/lib"
        assert env["PYINSTALLER_RESET_ENVIRONMENT"] == "1"

    def test_one_that_will_not_start_is_reported(self, install_dir, capsys):
        appimage, _downloaded = install_dir

        def refuse(command, **kwargs):
            raise OSError(errno.ENOEXEC, "Exec format error")

        assert linux_appimage.relaunch(appimage, refuse) is False
        assert "could not start" in capsys.readouterr().out


class TestSayingHowToUpdateByHand:
    """Each way of installing snipux has its own command, and the wrong
    one either fails or updates something the user is not running."""

    @pytest.fixture(autouse=True)
    def _machine(self, monkeypatch):
        monkeypatch.setattr(linux_appimage, "_machine", lambda: "x86_64")

    def test_an_appimage_names_the_file_and_where_it_goes(self, install_dir):
        appimage, _downloaded = install_dir

        hint = linux_appimage.manual_update_hint("2.0.0")

        assert "Snipux-2.0.0-x86_64.AppImage" in hint
        assert str(appimage) in hint

    def test_an_appimage_somewhere_read_only_says_why(self, install_dir, monkeypatch):
        appimage, _downloaded = install_dir
        monkeypatch.setattr(linux_appimage.os, "access", lambda path, mode: False)

        hint = linux_appimage.manual_update_hint("2.0.0")

        assert "cannot write to" in hint
        assert "the way it was installed" in hint
        assert str(appimage.parent) in hint

    def test_the_deb_names_the_package_and_apt(self, monkeypatch):
        monkeypatch.delenv("APPIMAGE", raising=False)
        monkeypatch.setattr(sys, "frozen", True, raising=False)

        hint = linux_appimage.manual_update_hint("2.0.0")

        assert "sudo apt install ./snipux_2.0.0_amd64.deb" in hint

    def test_pipx_is_upgraded_with_pipx(self, monkeypatch, tmp_path):
        monkeypatch.delenv("APPIMAGE", raising=False)
        monkeypatch.setattr(sys, "frozen", False, raising=False)
        monkeypatch.setattr(sys, "prefix", str(tmp_path / "pipx" / "venvs" / "snipux"))

        assert "pipx upgrade snipux" in linux_appimage.manual_update_hint("2.0.0")

    def test_pip_keeps_its_own_command(self, monkeypatch, tmp_path):
        monkeypatch.delenv("APPIMAGE", raising=False)
        monkeypatch.setattr(sys, "frozen", False, raising=False)
        monkeypatch.setattr(sys, "prefix", str(tmp_path / "venv"))

        assert "snipux --update" in linux_appimage.manual_update_hint("2.0.0")
