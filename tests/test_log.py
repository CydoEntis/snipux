"""`log.py`: handled failures, printed as before and kept in `snipux.log`."""

import sys

from snipux import __version__, log, setup_desktop


class TestNote:
    def test_it_is_still_printed(self, tmp_path, capsys):
        log.note("Note: could not write the shortcut", path=tmp_path / "snipux.log")

        assert capsys.readouterr().out == "Note: could not write the shortcut\n"

    def test_it_is_kept_in_the_notes_file_with_when_and_which_version(self, tmp_path):
        target = tmp_path / "snipux.log"

        log.note("Note: could not write the shortcut", path=target)
        log.note("Note: could not write the icon", path=target)

        lines = target.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 2
        assert lines[0].endswith(f"snipux {__version__}: Note: could not write the shortcut")
        assert lines[1].endswith("Note: could not write the icon")

    def test_the_file_sits_beside_the_config(self):
        log.note("Note: something handled")

        notes = setup_desktop.config_path().parent / "snipux.log"
        assert "Note: something handled" in notes.read_text(encoding="utf-8")

    def test_a_build_with_no_console_still_keeps_the_note(self, tmp_path, monkeypatch):
        # The windowless launcher and pythonw start with no stdout at all.
        monkeypatch.setattr(sys, "stdout", None)
        target = tmp_path / "snipux.log"

        log.note("Note: could not start the update installer", path=target)

        assert "could not start the update installer" in target.read_text(encoding="utf-8")

    def test_an_error_goes_to_stderr_when_asked(self, tmp_path, capsys):
        log.note("error: no launcher", to_stderr=True, path=tmp_path / "snipux.log")

        captured = capsys.readouterr()
        assert captured.err == "error: no launcher\n"
        assert captured.out == ""

    def test_a_file_that_cannot_be_written_does_not_raise(self, tmp_path, capsys):
        in_the_way = tmp_path / "in-the-way"
        in_the_way.write_text("")

        log.note("Note: nowhere to keep this", path=in_the_way / "snipux.log")

        assert "nowhere to keep this" in capsys.readouterr().out

    def test_a_file_past_the_limit_keeps_only_whole_recent_lines(self, tmp_path, monkeypatch):
        monkeypatch.setattr(log, "NOTES_LIMIT_BYTES", 4000)
        target = tmp_path / "snipux.log"

        for n in range(400):
            log.note(f"Note: failure number {n}", path=target)

        text = target.read_text(encoding="utf-8")
        assert "failure number 399\n" in text
        assert "failure number 0\n" not in text
        assert len(text.encode()) < 8000
        assert all(f"snipux {__version__}: Note: failure number" in line
                   for line in text.splitlines())
