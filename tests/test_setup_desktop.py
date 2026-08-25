"""SNX-73: `snipux --setup` -- the desktop entry, autostart entry, and
GNOME shortcut a pip/pipx install of the wheel cannot set up on its own.

No test here touches the real `~/.local/share`, `~/.config`, or `gsettings`
-- `run_setup()`'s directories and `bind_gnome_shortcut()`'s `subprocess`/
`shutil` calls are all either passed in explicitly or monkeypatched, the
same DI-or-monkeypatch pattern test_app.py already uses for
`copy_image_to_clipboard`'s `wl-copy` calls.
"""

from types import SimpleNamespace
from pathlib import Path

import pytest

from snipux import setup_desktop


class TestFindConsoleScript:
    def test_prefers_the_script_next_to_sys_executable(self, tmp_path, monkeypatch):
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        python = bin_dir / "python"
        python.write_text("")
        script = bin_dir / "snipux"
        script.write_text("")
        monkeypatch.setattr(setup_desktop.sys, "executable", str(python))

        found = setup_desktop.find_console_script()

        assert found == script.resolve()

    def test_falls_back_to_shutil_which_when_no_sibling_script_exists(
        self, tmp_path, monkeypatch
    ):
        # sys.executable points somewhere with no "snipux" next to it, so
        # the primary guess must not be trusted blindly.
        monkeypatch.setattr(setup_desktop.sys, "executable", str(tmp_path / "python"))
        which_target = tmp_path / "elsewhere" / "snipux"
        which_target.parent.mkdir()
        which_target.write_text("")
        monkeypatch.setattr(setup_desktop.shutil, "which", lambda name: str(which_target))

        found = setup_desktop.find_console_script()

        assert found == which_target.resolve()

    def test_returns_none_when_neither_guess_finds_anything(self, tmp_path, monkeypatch):
        monkeypatch.setattr(setup_desktop.sys, "executable", str(tmp_path / "python"))
        monkeypatch.setattr(setup_desktop.shutil, "which", lambda name: None)

        assert setup_desktop.find_console_script() is None


class TestRenderDesktopEntry:
    def test_replaces_the_placeholder_exec_line_with_the_real_path(self):
        # str(Path(...)), not a hardcoded "/opt/..." string -- development
        # happens on Windows too (per CLAUDE.md), where Path renders with
        # backslashes, and this only needs to prove the placeholder line
        # became the given path, not assume a separator.
        exec_path = Path("/opt/snipux/bin/snipux")

        rendered = setup_desktop.render_desktop_entry(exec_path)

        assert f"Exec={exec_path}" in rendered
        assert "__SNIPUX_LAUNCHER__" not in rendered
        # The rest of the bundled template is untouched -- this only ever
        # rewrites the one placeholder line.
        assert "Name=snipux" in rendered
        assert "Type=Application" in rendered
        # SNX-81: names our own icon (installed by install_icons() into the
        # hicolor theme), not org.gnome.Screenshot -- GNOME's own
        # screenshot tool's icon.
        assert "Icon=snipux" in rendered


class TestRunSetup:
    def test_reports_and_writes_the_desktop_and_autostart_entries(self, tmp_path, capsys):
        applications_dir = tmp_path / "applications"
        autostart_dir = tmp_path / "autostart"
        exec_path = Path("/opt/snipux/bin/snipux")

        exit_code = setup_desktop.run_setup(
            exec_path=exec_path,
            applications_dir=applications_dir,
            autostart_dir=autostart_dir,
            hicolor_dir=tmp_path / "icons",
        )

        assert exit_code == 0
        desktop_file = applications_dir / "snipux.desktop"
        autostart_file = autostart_dir / "snipux.desktop"
        assert desktop_file.read_text() == autostart_file.read_text()
        assert f"Exec={exec_path}" in desktop_file.read_text()

        out = capsys.readouterr().out
        assert f"Desktop entry written to {desktop_file}" in out
        assert f"Autostart entry written to {autostart_file}" in out

    def test_running_twice_leaves_exactly_one_entry_in_each_directory(self, tmp_path):
        applications_dir = tmp_path / "applications"
        autostart_dir = tmp_path / "autostart"
        exec_path = Path("/opt/snipux/bin/snipux")

        setup_desktop.run_setup(
            exec_path=exec_path,
            applications_dir=applications_dir,
            autostart_dir=autostart_dir,
            hicolor_dir=tmp_path / "icons",
        )
        setup_desktop.run_setup(
            exec_path=exec_path,
            applications_dir=applications_dir,
            autostart_dir=autostart_dir,
            hicolor_dir=tmp_path / "icons",
        )

        assert list(applications_dir.iterdir()) == [applications_dir / "snipux.desktop"]
        assert list(autostart_dir.iterdir()) == [autostart_dir / "snipux.desktop"]

    def test_returns_1_and_reports_when_the_console_script_cannot_be_found(
        self, monkeypatch, tmp_path, capsys
    ):
        monkeypatch.setattr(setup_desktop, "find_console_script", lambda: None)

        exit_code = setup_desktop.run_setup(
            applications_dir=tmp_path / "applications",
            autostart_dir=tmp_path / "autostart",
            hicolor_dir=tmp_path / "icons",
        )

        assert exit_code == 1
        err = capsys.readouterr().err
        assert "could not locate the installed snipux console script" in err
        # And nothing was written, since there is no real path to write.
        assert not (tmp_path / "applications").exists()

    def test_a_desktop_entry_write_failure_does_not_stop_the_autostart_step(
        self, tmp_path, monkeypatch, capsys
    ):
        # Simulates a read-only applications_dir -- one step failing must
        # not take the rest down with it, per CLAUDE.md's rule for capture
        # backends applied here.
        applications_dir = tmp_path / "applications"
        autostart_dir = tmp_path / "autostart"
        exec_path = Path("/opt/snipux/bin/snipux")

        real_mkdir = Path.mkdir

        def failing_mkdir(self, *args, **kwargs):
            if self == applications_dir:
                raise PermissionError("no")
            return real_mkdir(self, *args, **kwargs)

        monkeypatch.setattr(Path, "mkdir", failing_mkdir)

        exit_code = setup_desktop.run_setup(
            exec_path=exec_path,
            applications_dir=applications_dir,
            autostart_dir=autostart_dir,
            hicolor_dir=tmp_path / "icons",
        )

        assert exit_code == 0
        assert (autostart_dir / "snipux.desktop").exists()
        out = capsys.readouterr().out
        assert "Note: could not write the desktop entry" in out

    def test_installs_the_hicolor_icon_theme_entries(self, tmp_path):
        hicolor_dir = tmp_path / "icons"

        exit_code = setup_desktop.run_setup(
            exec_path=Path("/opt/snipux/bin/snipux"),
            applications_dir=tmp_path / "applications",
            autostart_dir=tmp_path / "autostart",
            hicolor_dir=hicolor_dir,
        )

        assert exit_code == 0
        installed = {path.name for path in hicolor_dir.glob("*/apps/snipux.png")}
        assert installed == {"snipux.png"}
        # One per vendored size, not just one -- GNOME's app list and the
        # window switcher each ask for a different resolution.
        sizes = {path.parent.parent.name for path in hicolor_dir.glob("*/apps/snipux.png")}
        vendored_sizes = {
            f"{path.stem.split('-')[1]}x{path.stem.split('-')[1]}"
            for path in setup_desktop._LOGO_DIR.glob("snipux-*.png")
        }
        assert sizes == vendored_sizes


class TestInstallIcons:
    """SNX-81: `install_icons()` is what places the vendored
    `design/logo/snipux-<size>.png` files into the layout GNOME's app list
    and window switcher actually search -- `hicolor_dir/<size>x<size>/
    apps/snipux.png` -- rather than `Icon=snipux` in the desktop entry
    resolving to nothing.
    """

    def test_copies_every_vendored_size_into_its_own_hicolor_directory(self, tmp_path):
        hicolor_dir = tmp_path / "hicolor"

        result = setup_desktop.install_icons(hicolor_dir)

        assert result is True
        for path in setup_desktop._LOGO_DIR.glob("snipux-*.png"):
            size = path.stem.split("-", 1)[1]
            installed = hicolor_dir / f"{size}x{size}" / "apps" / "snipux.png"
            assert installed.read_bytes() == path.read_bytes()

    def test_running_twice_leaves_the_same_single_file_per_size(self, tmp_path):
        hicolor_dir = tmp_path / "hicolor"

        setup_desktop.install_icons(hicolor_dir)
        setup_desktop.install_icons(hicolor_dir)

        for size_dir in hicolor_dir.iterdir():
            assert [p.name for p in (size_dir / "apps").iterdir()] == ["snipux.png"]

    def test_one_size_failing_to_write_does_not_stop_the_others(
        self, tmp_path, monkeypatch, capsys
    ):
        hicolor_dir = tmp_path / "hicolor"
        failing_size_dir = hicolor_dir / "16x16"

        real_mkdir = Path.mkdir

        def failing_mkdir(self, *args, **kwargs):
            if self == failing_size_dir / "apps":
                raise PermissionError("no")
            return real_mkdir(self, *args, **kwargs)

        monkeypatch.setattr(Path, "mkdir", failing_mkdir)

        result = setup_desktop.install_icons(hicolor_dir)

        assert result is True  # at least one other size still made it
        assert not (failing_size_dir / "apps" / "snipux.png").exists()
        assert (hicolor_dir / "32x32" / "apps" / "snipux.png").exists()
        out = capsys.readouterr().out
        assert "Note: could not install the 16x16 icon" in out

    def test_reports_and_returns_false_when_nothing_could_be_installed(
        self, tmp_path, monkeypatch, capsys
    ):
        monkeypatch.setattr(setup_desktop, "_LOGO_DIR", tmp_path / "no-such-logo-dir")

        result = setup_desktop.install_icons(tmp_path / "hicolor")

        assert result is False
        out = capsys.readouterr().out
        assert "no icon theme entries were written" in out


class TestRunRemove:
    """SNX-83: `run_remove()` is `run_setup()`'s exact counterpart --
    everything it writes, `--remove` deletes -- so `pipx uninstall snipux`
    doesn't leave a dead autostart entry, a dead keybinding, and a ghost
    application-list entry behind.
    """

    def test_removes_the_desktop_and_autostart_entries_and_reports_it(
        self, tmp_path, capsys
    ):
        applications_dir = tmp_path / "applications"
        autostart_dir = tmp_path / "autostart"
        exec_path = Path("/opt/snipux/bin/snipux")
        setup_desktop.run_setup(
            exec_path=exec_path,
            applications_dir=applications_dir,
            autostart_dir=autostart_dir,
            hicolor_dir=tmp_path / "icons",
        )
        capsys.readouterr()  # discard --setup's own output

        exit_code = setup_desktop.run_remove(
            applications_dir=applications_dir,
            autostart_dir=autostart_dir,
            hicolor_dir=tmp_path / "icons",
        )

        assert exit_code == 0
        assert not (applications_dir / "snipux.desktop").exists()
        assert not (autostart_dir / "snipux.desktop").exists()
        out = capsys.readouterr().out
        assert f"Desktop entry removed from {applications_dir / 'snipux.desktop'}" in out
        assert f"Autostart entry removed from {autostart_dir / 'snipux.desktop'}" in out

    def test_removes_the_installed_icons(self, tmp_path):
        hicolor_dir = tmp_path / "icons"
        setup_desktop.run_setup(
            exec_path=Path("/opt/snipux/bin/snipux"),
            applications_dir=tmp_path / "applications",
            autostart_dir=tmp_path / "autostart",
            hicolor_dir=hicolor_dir,
        )
        assert list(hicolor_dir.glob("*/apps/snipux.png"))  # sanity: setup wrote some

        setup_desktop.run_remove(
            applications_dir=tmp_path / "applications",
            autostart_dir=tmp_path / "autostart",
            hicolor_dir=hicolor_dir,
        )

        assert list(hicolor_dir.glob("*/apps/snipux.png")) == []

    def test_running_it_when_nothing_was_ever_set_up_reports_absence_not_failure(
        self, tmp_path, capsys, monkeypatch
    ):
        monkeypatch.setattr(setup_desktop.shutil, "which", lambda name: None)

        exit_code = setup_desktop.run_remove(
            applications_dir=tmp_path / "applications",
            autostart_dir=tmp_path / "autostart",
            hicolor_dir=tmp_path / "icons",
        )

        assert exit_code == 0
        out = capsys.readouterr().out
        assert "not found" in out
        assert "nothing to remove" in out.lower()

    def test_running_it_twice_is_harmless(self, tmp_path, monkeypatch):
        monkeypatch.setattr(setup_desktop.shutil, "which", lambda name: None)
        applications_dir = tmp_path / "applications"
        autostart_dir = tmp_path / "autostart"
        exec_path = Path("/opt/snipux/bin/snipux")
        setup_desktop.run_setup(
            exec_path=exec_path,
            applications_dir=applications_dir,
            autostart_dir=autostart_dir,
            hicolor_dir=tmp_path / "icons",
        )

        first = setup_desktop.run_remove(
            applications_dir=applications_dir,
            autostart_dir=autostart_dir,
            hicolor_dir=tmp_path / "icons",
        )
        second = setup_desktop.run_remove(
            applications_dir=applications_dir,
            autostart_dir=autostart_dir,
            hicolor_dir=tmp_path / "icons",
        )

        assert first == 0
        assert second == 0
        assert not (applications_dir / "snipux.desktop").exists()

    def test_a_step_that_cannot_be_done_still_lets_the_rest_complete(
        self, tmp_path, monkeypatch, capsys
    ):
        # Simulates a read-only applications_dir at removal time -- one step
        # failing must not take the rest down with it, per CLAUDE.md's rule
        # for capture backends applied here.
        applications_dir = tmp_path / "applications"
        autostart_dir = tmp_path / "autostart"
        exec_path = Path("/opt/snipux/bin/snipux")
        setup_desktop.run_setup(
            exec_path=exec_path,
            applications_dir=applications_dir,
            autostart_dir=autostart_dir,
            hicolor_dir=tmp_path / "icons",
        )

        real_unlink = Path.unlink

        def failing_unlink(self, *args, **kwargs):
            if self == applications_dir / "snipux.desktop":
                raise PermissionError("no")
            return real_unlink(self, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", failing_unlink)

        exit_code = setup_desktop.run_remove(
            applications_dir=applications_dir,
            autostart_dir=autostart_dir,
            hicolor_dir=tmp_path / "icons",
        )

        assert exit_code == 0
        assert not (autostart_dir / "snipux.desktop").exists()
        out = capsys.readouterr().out
        assert "Note: could not remove the desktop entry" in out


class TestRemoveIcons:
    def test_removes_every_installed_size_and_reports_it(self, tmp_path, capsys):
        hicolor_dir = tmp_path / "hicolor"
        setup_desktop.install_icons(hicolor_dir)
        capsys.readouterr()

        result = setup_desktop.remove_icons(hicolor_dir)

        assert result is True
        assert list(hicolor_dir.glob("*/apps/snipux.png")) == []
        out = capsys.readouterr().out
        assert "removed from" in out

    def test_reports_absence_rather_than_failing_when_nothing_was_installed(
        self, tmp_path, capsys
    ):
        hicolor_dir = tmp_path / "hicolor"

        result = setup_desktop.remove_icons(hicolor_dir)

        assert result is False
        out = capsys.readouterr().out
        assert "no icon theme entries were found" in out

    def test_running_it_twice_is_harmless(self, tmp_path):
        hicolor_dir = tmp_path / "hicolor"
        setup_desktop.install_icons(hicolor_dir)

        first = setup_desktop.remove_icons(hicolor_dir)
        second = setup_desktop.remove_icons(hicolor_dir)

        assert first is True
        assert second is False

    def test_one_size_failing_to_remove_does_not_stop_the_others(
        self, tmp_path, monkeypatch, capsys
    ):
        hicolor_dir = tmp_path / "hicolor"
        setup_desktop.install_icons(hicolor_dir)
        capsys.readouterr()
        failing_target = hicolor_dir / "16x16" / "apps" / "snipux.png"

        real_unlink = Path.unlink

        def failing_unlink(self, *args, **kwargs):
            if self == failing_target:
                raise PermissionError("no")
            return real_unlink(self, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", failing_unlink)

        result = setup_desktop.remove_icons(hicolor_dir)

        assert result is True  # at least one other size still got removed
        assert failing_target.exists()
        assert not (hicolor_dir / "32x32" / "apps" / "snipux.png").exists()
        out = capsys.readouterr().out
        assert "Note: could not remove the 16x16 icon" in out


class TestAppendSlot:
    SLOT = setup_desktop._SLOT_PATH

    def test_appends_to_an_empty_list(self):
        assert setup_desktop._append_slot("@as []") == f"['{self.SLOT}']"

    def test_appends_to_an_existing_populated_list(self):
        result = setup_desktop._append_slot("['/existing/slot/']")

        assert result == f"['/existing/slot/', '{self.SLOT}']"

    def test_reuses_the_slot_if_already_present(self):
        current = f"['{self.SLOT}']"

        assert setup_desktop._append_slot(current) == current


class FakeGsettingsStore:
    """A tiny in-memory stand-in for the real `gsettings` binary, keyed the
    same way real calls are: (schema, key) -> the string gsettings would
    have printed for `get`.
    """

    def __init__(self):
        self.values = {
            (setup_desktop._MEDIA_KEYS_SCHEMA, "custom-keybindings"): "@as []"
        }
        self.calls = []

    def run(self, argv, **kwargs):
        self.calls.append(argv)
        if argv[1] == "get":
            schema, key = argv[2], argv[3]
            return SimpleNamespace(stdout=self.values.get((schema, key), "@as []"))
        if argv[1] == "set":
            schema, key, value = argv[2], argv[3], argv[4]
            self.values[(schema, key)] = value
            return SimpleNamespace(returncode=0)
        if argv[1] == "reset-recursively":
            schema = argv[2]
            for key in [key for key in self.values if key[0] == schema]:
                del self.values[key]
            return SimpleNamespace(returncode=0)
        raise AssertionError(f"unexpected gsettings invocation: {argv}")


class TestBindGnomeShortcut:
    def test_reports_a_note_when_gsettings_is_not_on_path(self, monkeypatch):
        monkeypatch.setattr(setup_desktop.shutil, "which", lambda name: None)

        message = setup_desktop.bind_gnome_shortcut(Path("/opt/snipux/bin/snipux"))

        assert "gsettings not found" in message

    def test_binds_the_shortcut_to_the_given_exec_path(self, monkeypatch):
        monkeypatch.setattr(setup_desktop.shutil, "which", lambda name: "/usr/bin/gsettings")
        store = FakeGsettingsStore()
        monkeypatch.setattr(setup_desktop.subprocess, "run", store.run)
        exec_path = Path("/opt/snipux/bin/snipux")

        message = setup_desktop.bind_gnome_shortcut(exec_path)

        assert f"Bound Super+Shift+S to run: {exec_path} --snip" in message
        assert store.values[(setup_desktop._SLOT_SCHEMA, "command")] == f"{exec_path} --snip"
        assert store.values[(setup_desktop._SLOT_SCHEMA, "binding")] == "<Super><Shift>s"
        assert store.values[(setup_desktop._SLOT_SCHEMA, "name")] == "snipux"

    def test_running_twice_leaves_the_slot_listed_exactly_once(self, monkeypatch):
        monkeypatch.setattr(setup_desktop.shutil, "which", lambda name: "/usr/bin/gsettings")
        store = FakeGsettingsStore()
        monkeypatch.setattr(setup_desktop.subprocess, "run", store.run)
        exec_path = Path("/opt/snipux/bin/snipux")

        setup_desktop.bind_gnome_shortcut(exec_path)
        setup_desktop.bind_gnome_shortcut(exec_path)

        final_list = store.values[(setup_desktop._MEDIA_KEYS_SCHEMA, "custom-keybindings")]
        assert final_list.count(setup_desktop._SLOT_PATH) == 1

    def test_keeps_a_shortcut_the_user_already_configured(self, monkeypatch):
        monkeypatch.setattr(setup_desktop.shutil, "which", lambda name: "/usr/bin/gsettings")
        store = FakeGsettingsStore()
        store.values[(setup_desktop._MEDIA_KEYS_SCHEMA, "custom-keybindings")] = (
            "['/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/custom0/']"
        )
        monkeypatch.setattr(setup_desktop.subprocess, "run", store.run)

        setup_desktop.bind_gnome_shortcut(Path("/opt/snipux/bin/snipux"))

        final_list = store.values[(setup_desktop._MEDIA_KEYS_SCHEMA, "custom-keybindings")]
        assert "custom0" in final_list
        assert setup_desktop._SLOT_PATH in final_list

    def test_reports_a_note_when_reading_the_list_fails(self, monkeypatch):
        monkeypatch.setattr(setup_desktop.shutil, "which", lambda name: "/usr/bin/gsettings")

        def raising_run(argv, **kwargs):
            raise setup_desktop.subprocess.CalledProcessError(1, argv)

        monkeypatch.setattr(setup_desktop.subprocess, "run", raising_run)

        message = setup_desktop.bind_gnome_shortcut(Path("/opt/snipux/bin/snipux"))

        assert "could not read GNOME's custom-keybindings list" in message

    def test_reports_a_note_when_setting_the_shortcut_fails(self, monkeypatch):
        monkeypatch.setattr(setup_desktop.shutil, "which", lambda name: "/usr/bin/gsettings")

        def flaky_run(argv, **kwargs):
            if argv[1] == "get":
                return SimpleNamespace(stdout="@as []")
            raise setup_desktop.subprocess.CalledProcessError(1, argv)

        monkeypatch.setattr(setup_desktop.subprocess, "run", flaky_run)

        message = setup_desktop.bind_gnome_shortcut(Path("/opt/snipux/bin/snipux"))

        assert "setting the GNOME shortcut failed" in message


class TestRemoveSlot:
    SLOT = setup_desktop._SLOT_PATH

    def test_returns_the_list_unchanged_when_the_slot_is_absent(self):
        current = "['/existing/slot/']"

        assert setup_desktop._remove_slot(current) == current

    def test_removes_the_only_entry_down_to_an_empty_list(self):
        assert setup_desktop._remove_slot(f"['{self.SLOT}']") == "@as []"

    def test_removes_the_slot_from_the_end_keeping_the_others(self):
        result = setup_desktop._remove_slot(f"['/existing/slot/', '{self.SLOT}']")

        assert result == "['/existing/slot/']"

    def test_removes_the_slot_from_the_start_keeping_the_others(self):
        result = setup_desktop._remove_slot(f"['{self.SLOT}', '/existing/slot/']")

        assert result == "['/existing/slot/']"

    def test_removes_the_slot_from_the_middle_keeping_the_others_in_order(self):
        result = setup_desktop._remove_slot(
            f"['/a/', '{self.SLOT}', '/b/']"
        )

        assert result == "['/a/', '/b/']"


class TestUnbindGnomeShortcut:
    def test_reports_a_note_when_gsettings_is_not_on_path(self, monkeypatch):
        monkeypatch.setattr(setup_desktop.shutil, "which", lambda name: None)

        message = setup_desktop.unbind_gnome_shortcut()

        assert "gsettings not found" in message
        assert "nothing to remove" in message.lower()

    def test_reports_plainly_when_the_shortcut_was_never_set(self, monkeypatch):
        monkeypatch.setattr(setup_desktop.shutil, "which", lambda name: "/usr/bin/gsettings")
        store = FakeGsettingsStore()
        monkeypatch.setattr(setup_desktop.subprocess, "run", store.run)

        message = setup_desktop.unbind_gnome_shortcut()

        assert "was not set" in message
        assert "nothing to remove" in message.lower()
        # Nothing was written back -- there was nothing to change.
        assert not any(call[1] == "set" for call in store.calls)

    def test_removes_the_slot_and_reports_it(self, monkeypatch):
        monkeypatch.setattr(setup_desktop.shutil, "which", lambda name: "/usr/bin/gsettings")
        store = FakeGsettingsStore()
        store.values[(setup_desktop._MEDIA_KEYS_SCHEMA, "custom-keybindings")] = (
            f"['{setup_desktop._SLOT_PATH}']"
        )
        store.values[(setup_desktop._SLOT_SCHEMA, "name")] = "snipux"
        monkeypatch.setattr(setup_desktop.subprocess, "run", store.run)

        message = setup_desktop.unbind_gnome_shortcut()

        assert "Removed the Super+Shift+S shortcut" in message
        final_list = store.values[(setup_desktop._MEDIA_KEYS_SCHEMA, "custom-keybindings")]
        assert setup_desktop._SLOT_PATH not in final_list
        # reset-recursively cleared the slot's own keys.
        assert (setup_desktop._SLOT_SCHEMA, "name") not in store.values

    def test_keeps_a_shortcut_the_user_configured_by_hand(self, monkeypatch):
        monkeypatch.setattr(setup_desktop.shutil, "which", lambda name: "/usr/bin/gsettings")
        store = FakeGsettingsStore()
        store.values[(setup_desktop._MEDIA_KEYS_SCHEMA, "custom-keybindings")] = (
            f"['/existing/custom0/', '{setup_desktop._SLOT_PATH}']"
        )
        monkeypatch.setattr(setup_desktop.subprocess, "run", store.run)

        setup_desktop.unbind_gnome_shortcut()

        final_list = store.values[(setup_desktop._MEDIA_KEYS_SCHEMA, "custom-keybindings")]
        assert "custom0" in final_list
        assert setup_desktop._SLOT_PATH not in final_list

    def test_running_it_twice_is_harmless(self, monkeypatch):
        monkeypatch.setattr(setup_desktop.shutil, "which", lambda name: "/usr/bin/gsettings")
        store = FakeGsettingsStore()
        store.values[(setup_desktop._MEDIA_KEYS_SCHEMA, "custom-keybindings")] = (
            f"['{setup_desktop._SLOT_PATH}']"
        )
        monkeypatch.setattr(setup_desktop.subprocess, "run", store.run)

        first = setup_desktop.unbind_gnome_shortcut()
        second = setup_desktop.unbind_gnome_shortcut()

        assert "Removed" in first
        assert "was not set" in second

    def test_reports_a_note_when_reading_the_list_fails(self, monkeypatch):
        monkeypatch.setattr(setup_desktop.shutil, "which", lambda name: "/usr/bin/gsettings")

        def raising_run(argv, **kwargs):
            raise setup_desktop.subprocess.CalledProcessError(1, argv)

        monkeypatch.setattr(setup_desktop.subprocess, "run", raising_run)

        message = setup_desktop.unbind_gnome_shortcut()

        assert "could not read GNOME's custom-keybindings list" in message

    def test_reports_a_note_when_removing_the_shortcut_fails(self, monkeypatch):
        monkeypatch.setattr(setup_desktop.shutil, "which", lambda name: "/usr/bin/gsettings")

        def flaky_run(argv, **kwargs):
            if argv[1] == "get":
                return SimpleNamespace(
                    stdout=f"['{setup_desktop._SLOT_PATH}']"
                )
            raise setup_desktop.subprocess.CalledProcessError(1, argv)

        monkeypatch.setattr(setup_desktop.subprocess, "run", flaky_run)

        message = setup_desktop.unbind_gnome_shortcut()

        assert "removing the GNOME shortcut failed" in message


class TestShortcutConfig:
    """The remembered shortcut: `--setup --shortcut` binds and persists it,
    and every later `--setup` (every `install.sh` performs one) keeps it
    rather than reverting to the default.
    """

    def test_defaults_when_nothing_is_stored(self, tmp_path):
        assert setup_desktop.load_shortcut(tmp_path) == setup_desktop.DEFAULT_SHORTCUT

    def test_a_saved_shortcut_round_trips(self, tmp_path):
        assert setup_desktop.save_shortcut("<Super><Shift>x", tmp_path)

        assert setup_desktop.load_shortcut(tmp_path) == "<Super><Shift>x"

    def test_a_corrupt_config_falls_back_to_the_default(self, tmp_path):
        # A broken config must never be able to fail --setup, which
        # install.sh runs on every install.
        path = setup_desktop.config_path(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{ this is not json")

        assert setup_desktop.load_shortcut(tmp_path) == setup_desktop.DEFAULT_SHORTCUT

    def test_a_stored_value_that_no_longer_validates_is_ignored(self, tmp_path):
        path = setup_desktop.config_path(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"shortcut": "Super+Shift+S"}')

        assert setup_desktop.load_shortcut(tmp_path) == setup_desktop.DEFAULT_SHORTCUT

    def test_saving_preserves_other_keys_in_the_document(self, tmp_path):
        path = setup_desktop.config_path(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"something_else": 42}')

        setup_desktop.save_shortcut("<Alt>Print", tmp_path)

        import json

        assert json.loads(path.read_text()) == {
            "something_else": 42,
            "shortcut": "<Alt>Print",
        }

    def test_forget_removes_the_file(self, tmp_path):
        setup_desktop.save_shortcut("<Alt>Print", tmp_path)

        assert setup_desktop.forget_shortcut(tmp_path)
        assert not setup_desktop.config_path(tmp_path).exists()

    def test_forget_is_a_no_op_when_nothing_was_stored(self, tmp_path):
        assert setup_desktop.forget_shortcut(tmp_path) is False


class TestShortcutValidation:
    """gsettings accepts any string and silently never fires a binding it
    cannot parse -- exactly the invisible failure this feature exists to
    escape -- so bad input is caught here instead.
    """

    @pytest.mark.parametrize(
        "accelerator", ["<Super><Shift>x", "<Alt>Print", "Print", "F9", "<Primary><Alt>p"]
    )
    def test_accepts_real_accelerators(self, accelerator):
        assert setup_desktop.validate_shortcut(accelerator) is None

    def test_rejects_the_human_readable_form_people_actually_type(self):
        problem = setup_desktop.validate_shortcut("Super+Shift+S")

        assert problem is not None
        assert "+" in problem and "<Super><Shift>x" in problem

    @pytest.mark.parametrize("bad", ["", "   ", "<Super> x", "<Super><Shift>"])
    def test_rejects_malformed_input(self, bad):
        assert setup_desktop.validate_shortcut(bad) is not None


class TestHumanShortcut:
    @pytest.mark.parametrize(
        "accelerator,expected",
        [
            ("<Super><Shift>s", "Super+Shift+S"),
            ("<Super><Shift>x", "Super+Shift+X"),
            ("<Alt>Print", "Alt+Print"),
            ("Print", "Print"),
        ],
    )
    def test_renders_the_way_docs_and_settings_panels_do(self, accelerator, expected):
        assert setup_desktop.human_shortcut(accelerator) == expected


class TestRunSetupWithAShortcut:
    """`--setup --shortcut` end to end, including the reason the config
    file exists at all: surviving the next `--setup`.
    """

    def _setup(self, tmp_path, **kwargs):
        return setup_desktop.run_setup(
            exec_path=Path("/opt/snipux/bin/snipux"),
            applications_dir=tmp_path / "applications",
            autostart_dir=tmp_path / "autostart",
            hicolor_dir=tmp_path / "icons",
            config_dir=tmp_path / "config",
            **kwargs,
        )

    def test_a_given_shortcut_is_bound_and_remembered(self, tmp_path, monkeypatch):
        bound = []
        monkeypatch.setattr(
            setup_desktop,
            "bind_gnome_shortcut",
            lambda exec_path, shortcut=None: bound.append(shortcut) or "bound",
        )

        exit_code = self._setup(tmp_path, shortcut="<Super><Shift>x")

        assert exit_code == 0
        assert bound == ["<Super><Shift>x"]
        assert setup_desktop.load_shortcut(tmp_path / "config") == "<Super><Shift>x"

    def test_a_later_setup_keeps_it_instead_of_reverting(self, tmp_path, monkeypatch):
        # The whole point: install.sh runs --setup on every install, and
        # without the stored value that would stomp the user's choice back
        # to Super+Shift+S every time.
        bound = []
        monkeypatch.setattr(
            setup_desktop,
            "bind_gnome_shortcut",
            lambda exec_path, shortcut=None: bound.append(shortcut) or "bound",
        )
        self._setup(tmp_path, shortcut="<Alt>Print")

        self._setup(tmp_path)

        assert bound == ["<Alt>Print", "<Alt>Print"]

    def test_a_bad_shortcut_fails_before_anything_is_written(self, tmp_path, capsys):
        exit_code = self._setup(tmp_path, shortcut="Super+Shift+S")

        assert exit_code == 1
        assert "error:" in capsys.readouterr().err
        assert not (tmp_path / "applications").exists()
        assert not setup_desktop.config_path(tmp_path / "config").exists()

    def test_remove_forgets_the_stored_shortcut(self, tmp_path, monkeypatch):
        monkeypatch.setattr(setup_desktop, "unbind_gnome_shortcut", lambda: "unbound")
        setup_desktop.save_shortcut("<Alt>Print", tmp_path / "config")

        setup_desktop.run_remove(
            applications_dir=tmp_path / "applications",
            autostart_dir=tmp_path / "autostart",
            hicolor_dir=tmp_path / "icons",
            config_dir=tmp_path / "config",
        )

        assert not setup_desktop.config_path(tmp_path / "config").exists()
