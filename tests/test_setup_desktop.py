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
