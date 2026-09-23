"""`output.py`: where a finished capture goes.

The clipboard helpers are exercised through the windows that call them
(test_review.py, test_player.py); what is here is the file-writing half,
which had no test module of its own -- and which is where a failed write
used to be reported to the user as a success.
"""

import os
from pathlib import Path

import pytest
from PyQt6.QtGui import QImage

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
