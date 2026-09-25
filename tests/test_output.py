"""`output.py`: where a finished capture goes.

The clipboard helpers are exercised through the windows that call them
(test_review.py, test_player.py); what is here is the file-writing half,
which had no test module of its own -- and which is where a failed write
used to be reported to the user as a success.
"""

import base64
import os
from pathlib import Path

import pytest
from PyQt6.QtGui import QGuiApplication, QImage
from PyQt6.QtWidgets import QApplication


@pytest.fixture(scope="module", autouse=True)
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app

from snipux import output


def make_image(width: int = 20, height: int = 12) -> QImage:
    image = QImage(width, height, QImage.Format.Format_RGB32)
    image.fill(0xFF3366CC)
    return image


class TestSaveImage:
    def test_writes_the_file_and_returns_where(self, tmp_path):
        path = output.save_image(make_image(), tmp_path)

        assert path.parent == tmp_path
        assert path.suffix == ".png"
        assert path.stat().st_size > 0

    def test_creates_the_directory_it_was_given(self, tmp_path):
        directory = tmp_path / "not" / "there" / "yet"

        path = output.save_image(make_image(), directory)

        assert path.parent == directory

    def test_a_write_that_fails_raises_rather_than_returning_a_path(self, tmp_path):
        # QImage.save() reports failure by returning False. That return used
        # to be discarded, so a full disk or a read-only folder produced a
        # path to a file that was never written -- which the overlay toasted
        # as "Saved to ..." and app.py added to the Recent list. A null image
        # is the reproducible stand-in for any write Qt refuses.
        with pytest.raises(OSError):
            output.save_image(QImage(), tmp_path)

    def test_the_failure_names_the_file(self, tmp_path):
        with pytest.raises(OSError) as raised:
            output.save_image(QImage(), tmp_path)

        assert str(tmp_path) in str(raised.value)


class TestDisplayPath:
    def test_a_path_under_home_is_shown_relative_to_it(self, monkeypatch, tmp_path):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        shown = output.display_path(tmp_path / "Pictures" / "snipux" / "snip.png")

        assert shown == f"~{os.sep}Pictures{os.sep}snipux{os.sep}snip.png"

    def test_it_uses_the_platform_separator_throughout(self, monkeypatch, tmp_path):
        # The player built this with a literal "~/" while the rest of the
        # path came from relative_to(), which renders with the platform's
        # own separator -- so on Windows one path was spelled two ways in a
        # single string, in the label whose job is saying where a file is.
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        shown = output.display_path(tmp_path / "Videos" / "clip.webm")

        assert "/" not in shown.replace(os.sep, "") or os.sep == "/"
        assert shown.count(os.sep) == 2

    def test_a_path_outside_home_is_shown_whole(self, monkeypatch, tmp_path):
        monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
        elsewhere = tmp_path / "elsewhere" / "snip.png"

        assert output.display_path(elsewhere) == str(elsewhere)

    def test_nothing_written_says_so(self):
        assert output.display_path(None) == "Not saved to disk"


class TestTerminalPasteCommand:
    """A clipboard cannot cross an SSH connection: what a terminal receives
    is keystrokes, so the only thing that can travel is text. This is the
    image as text, in a form the far end rebuilds with nothing installed on
    it.
    """

    def test_it_is_a_command_that_writes_the_file(self):
        command = output.terminal_paste_command(make_image(), "snip.png")

        assert command.startswith("base64 -d > 'snip.png' <<'SNIPUX'")
        assert command.rstrip().endswith("SNIPUX")

    def test_the_body_rebuilds_the_image_exactly(self):
        image = make_image(48, 32)
        command = output.terminal_paste_command(image, "snip.png")

        body = command.splitlines()[1:-1]
        rebuilt_bytes = base64.b64decode("".join(body))
        rebuilt = QImage()
        rebuilt.loadFromData(rebuilt_bytes, "PNG")

        assert rebuilt.size() == image.size()
        assert rebuilt.pixel(4, 4) == image.pixel(4, 4)

    def test_no_line_is_long_enough_for_a_terminal_to_drop(self):
        # A terminal in canonical mode stops accepting a line somewhere
        # around 4 KB and discards the rest *silently* -- the paste looks
        # fine and the file on the far end is truncated.
        command = output.terminal_paste_command(make_image(400, 300), "snip.png")

        assert max(len(line) for line in command.splitlines()) <= 76

    def test_the_heredoc_is_quoted_so_the_far_shell_expands_nothing(self):
        # <<'SNIPUX', not <<SNIPUX: base64's alphabet contains none of the
        # characters a shell would expand, but relying on that is relying on
        # an accident of the data.
        command = output.terminal_paste_command(make_image(), "snip.png")

        assert "<<'SNIPUX'" in command

    def test_a_filename_is_quoted_so_a_space_cannot_split_it(self):
        command = output.terminal_paste_command(make_image(), "my snip.png")

        assert "> 'my snip.png' <<" in command

    def test_it_names_a_file_when_the_caller_does_not(self):
        command = output.terminal_paste_command(make_image())

        assert command.startswith("base64 -d > 'snip-")
        assert ".png' <<" in command

    def test_too_big_to_paste_is_refused_rather_than_produced(self):
        # Tens of thousands of lines take long enough that a terminal looks
        # hung, and some refuse a paste that size outright. The caller says
        # so rather than copying something that will not work.
        huge = QImage(4000, 4000, QImage.Format.Format_RGB32)
        for y in range(0, 4000, 2):        # noise, so PNG cannot compress it away
            for x in range(0, 4000, 2):
                huge.setPixel(x, y, (x * 2654435761 + y * 40503) & 0xFFFFFF)

        assert output.terminal_paste_command(huge) is None


class TestCopyingBothFormsAtOnce:
    """One Copy, two forms on one clipboard entry: the application being
    pasted into picks the one it understands. An image editor takes the
    picture and never sees the text; a terminal cannot take a picture at
    all, so it takes the text.
    """

    def test_an_image_alone_by_default(self):
        output.copy_image_to_clipboard(make_image())

        mime = QGuiApplication.clipboard().mimeData()
        assert mime.hasImage()
        assert not mime.text()

    def test_both_when_the_text_is_given(self):
        image = make_image()

        output.copy_image_to_clipboard(image, also_as_text="base64 -d > 'x.png'")

        mime = QGuiApplication.clipboard().mimeData()
        assert mime.hasImage(), "an image editor must still get the picture"
        assert mime.text() == "base64 -d > 'x.png'", "a terminal must get the text"
