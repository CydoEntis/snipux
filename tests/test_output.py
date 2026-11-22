"""`output.py`: where a finished capture goes.

The clipboard helpers are exercised through the windows that call them
(test_review.py, test_player.py); what is here is the file-writing half,
which had no test module of its own -- and which is where a failed write
used to be reported to the user as a success.
"""

import os
import threading
import time
from pathlib import Path

import pytest
from PyQt6.QtGui import QImage
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication

from snipux import output


def make_image(width: int = 20, height: int = 12) -> QImage:
    image = QImage(width, height, QImage.Format.Format_RGB32)
    image.fill(0xFF3366CC)
    return image


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def wait_until(condition, timeout_ms: int = 10_000) -> None:
    """Run the event loop until `condition()` holds, and fail rather than
    hang if it never does."""
    deadline = time.monotonic() + timeout_ms / 1000
    while not condition():
        assert time.monotonic() < deadline, "waited too long"
        QTest.qWait(1)


class TestWriteImage:
    def test_a_failed_write_leaves_nothing_under_either_name(self, tmp_path):
        path = tmp_path / "snip.png"

        with pytest.raises(OSError):
            output.write_image(QImage(), path)

        assert list(tmp_path.iterdir()) == []

    def test_the_real_name_never_holds_a_partial_file(self, tmp_path, monkeypatch):
        # Quitting part-way through must not leave a truncated PNG where
        # the snip was meant to be: the bytes go to another name first.
        path = tmp_path / "snip.png"
        seen_while_encoding = []
        really_save = QImage.save

        def save(image, target, fmt):
            seen_while_encoding.append((Path(target), path.exists()))
            return really_save(image, target, fmt)

        monkeypatch.setattr(QImage, "save", save)

        output.write_image(make_image(), path)

        [(target, real_name_existed)] = seen_while_encoding
        assert target != path and target.parent == path.parent
        assert not real_name_existed
        assert [entry.name for entry in tmp_path.iterdir()] == ["snip.png"]


class TestSaveImageLater:
    """`save_image_later`: the name now, the bytes on a worker thread, the
    answer back on the UI thread. The write is held by a stand-in that
    waits for the test, never by a sleep."""

    @pytest.fixture
    def held(self, qapp, monkeypatch):
        go = threading.Event()
        really_write = output.write_image

        def write_image(image, path):
            assert go.wait(10), "the test never let the write finish"
            really_write(image, path)

        monkeypatch.setattr(output, "write_image", write_image)
        yield go
        go.set()
        wait_until(lambda: not output.saves_in_flight())

    def test_it_returns_the_path_before_the_file_is_written(self, held, tmp_path):
        landed = []

        path = output.save_image_later(
            make_image(), tmp_path, pattern="snip", on_written=lambda *a: landed.append(a)
        )

        assert path == tmp_path / "snip.png"
        assert not path.exists() and landed == []
        held.set()
        wait_until(lambda: landed)
        assert landed == [(path, None)]
        assert not QImage(str(path)).isNull()

    def test_the_answer_comes_back_on_the_calling_thread(self, held, tmp_path):
        threads = []
        output.save_image_later(
            make_image(), tmp_path, on_written=lambda *a: threads.append(threading.current_thread())
        )

        held.set()
        wait_until(lambda: threads)

        assert threads == [threading.current_thread()]

    def test_two_saves_in_one_second_never_share_a_name(self, held, tmp_path):
        # Nothing is on disk under the first name yet to say it is taken.
        landed = []
        first = output.save_image_later(
            make_image(), tmp_path, pattern="snip", on_written=lambda *a: landed.append(a)
        )
        second = output.save_image_later(
            make_image(), tmp_path, pattern="snip", on_written=lambda *a: landed.append(a)
        )
        held.set()
        third = output.save_image(make_image(), tmp_path, pattern="snip")
        wait_until(lambda: len(landed) == 2)

        assert len({first, second, third}) == 3
        assert all(path.is_file() for path in (first, second, third))

    def test_a_name_is_free_again_once_its_write_has_failed(self, qapp, tmp_path):
        landed = []
        first = output.save_image_later(
            QImage(), tmp_path, pattern="snip", on_written=lambda *a: landed.append(a)
        )
        wait_until(lambda: landed)

        [(path, error)] = landed
        assert path == first and isinstance(error, OSError)
        assert list(tmp_path.iterdir()) == []
        assert output.save_image(make_image(), tmp_path, pattern="snip") == first

    def test_a_folder_that_cannot_be_made_raises_before_anything_starts(self, qapp, tmp_path):
        blocker = tmp_path / "not-a-folder"
        blocker.write_text("in the way")

        with pytest.raises(OSError):
            output.save_image_later(
                make_image(), blocker / "snips", on_written=lambda *a: pytest.fail("reported")
            )

        assert output.saves_in_flight() == 0


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

    def test_the_name_comes_from_the_pattern_in_settings(self, tmp_path):
        # Settings stored the pattern and previewed it; nothing read it.
        from snipux import setup_desktop

        setup_desktop.save_filename_pattern("Bug %Y")

        path = output.save_image(make_image(), tmp_path)

        assert path.name.startswith("Bug 20")
        assert path.suffix == ".png"

    def test_a_pattern_nothing_can_format_falls_back_to_the_default(
        self, tmp_path, monkeypatch
    ):
        from snipux import setup_desktop

        real = setup_desktop.preview_filename

        def picky(folder, pattern, extension="png"):
            if pattern == "broken":
                raise ValueError("bad pattern")
            return real(folder, pattern, extension)

        monkeypatch.setattr(setup_desktop, "preview_filename", picky)

        path = output.save_image(make_image(), tmp_path, pattern="broken")

        assert path.name.startswith("Screenshot from ")

    def test_a_second_save_under_the_same_name_keeps_the_first(self, tmp_path):
        first = output.save_image(make_image(), tmp_path, pattern="snip")
        blue = make_image()
        blue.fill(0xFF0000FF)

        second = output.save_image(blue, tmp_path, pattern="snip")
        third = output.save_image(blue, tmp_path, pattern="snip")

        assert first.name == "snip.png"
        assert second.name == "snip (2).png"
        assert third.name == "snip (3).png"
        assert QImage(str(first)).pixel(0, 0) == 0xFF3366CC

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


class TestCopyHistory:
    """The last few copies, held in memory so one can be copied again."""

    @pytest.fixture(autouse=True)
    def _clipboard_without_wl_copy(self, qapp_for_clipboard, monkeypatch):
        # The clipboard needs an application, and a runner that happens to
        # have wl-copy must not have these tests start it.
        monkeypatch.setattr(output.shutil, "which", lambda name: None)

    def test_newest_first_across_all_three_kinds(self, tmp_path):
        output.copy_text_to_clipboard("hello")
        output.copy_image_to_clipboard(make_image())
        output.copy_file_to_clipboard(tmp_path / "clip.webm")

        assert [item.kind for item in output.copy_history()] == ["file", "image", "text"]

    def test_only_the_last_ten_are_kept(self):
        for index in range(output.COPY_HISTORY_MAX + 3):
            output.copy_text_to_clipboard(f"copy {index}")

        values = [item.value for item in output.copy_history()]
        assert len(values) == output.COPY_HISTORY_MAX
        assert values[0] == f"copy {output.COPY_HISTORY_MAX + 2}"
        assert "copy 0" not in values

    def test_pictures_past_the_memory_budget_drop_the_oldest(self, monkeypatch):
        # Ten full-desktop copies at 5120x2880 were 590 MB held until quit.
        one = make_image(100, 100).sizeInBytes()
        monkeypatch.setattr(output, "COPY_HISTORY_MAX_BYTES", 3 * one)

        for shade in range(5):
            image = make_image(100, 100)
            image.fill(0xFF000000 + shade)
            output.copy_image_to_clipboard(image)

        kept = [item.value.pixel(0, 0) & 0xFF for item in output.copy_history()]
        assert kept == [4, 3, 2]

    def test_the_newest_copy_is_kept_even_when_it_alone_is_over_budget(self, monkeypatch):
        monkeypatch.setattr(output, "COPY_HISTORY_MAX_BYTES", 1000)
        output.copy_image_to_clipboard(make_image(90, 90))
        output.copy_text_to_clipboard("small and old")

        output.copy_image_to_clipboard(make_image(100, 100))

        assert [item.kind for item in output.copy_history()] == ["image", "text"]
        assert output.copy_history()[0].value.width() == 100

    def test_text_and_files_do_not_use_up_the_picture_budget(self, monkeypatch, tmp_path):
        one = make_image(100, 100).sizeInBytes()
        monkeypatch.setattr(output, "COPY_HISTORY_MAX_BYTES", one + 1000)
        output.copy_image_to_clipboard(make_image(100, 100))

        output.copy_text_to_clipboard("hello")
        output.copy_file_to_clipboard(tmp_path / "clip.webm")

        assert [item.kind for item in output.copy_history()] == ["file", "text", "image"]

    def test_the_kept_image_is_not_the_callers(self):
        # The caller's image may be backed by a buffer it is about to free.
        image = make_image()
        output.copy_image_to_clipboard(image)
        image.fill(0xFF000000)

        assert output.copy_history()[0].value.pixel(0, 0) == 0xFF3366CC

    def test_recopying_puts_it_back_and_moves_it_to_the_top(self, qapp_for_clipboard):
        output.copy_text_to_clipboard("first")
        output.copy_text_to_clipboard("second")

        output.recopy(output.copy_history()[1])

        assert qapp_for_clipboard.clipboard().text() == "first"
        assert [item.value for item in output.copy_history()] == ["first", "second"]

    def test_clearing_empties_it(self):
        output.copy_text_to_clipboard("gone")

        output.clear_copy_history()

        assert output.copy_history() == []

    def test_the_listener_hears_every_change(self):
        heard = []
        output.set_copy_history_listener(lambda: heard.append(len(output.copy_history())))

        output.copy_text_to_clipboard("one")
        output.clear_copy_history()

        assert heard == [1, 0]


class TestWlCopyThatDoesNotAnswer:
    """`wl-copy` runs on the UI thread; a compositor that never takes the
    selection used to hang Snipux there for good."""

    @pytest.fixture(autouse=True)
    def _wl_copy_that_hangs(self, qapp_for_clipboard, monkeypatch):
        import subprocess

        self.timeouts = []

        def hangs(argv, input=None, check=None, timeout=None):
            self.timeouts.append(timeout)
            raise subprocess.TimeoutExpired(argv, timeout)

        monkeypatch.setattr(output.shutil, "which", lambda name: f"/usr/bin/{name}")
        monkeypatch.setattr(output.subprocess, "run", hangs)

    def test_a_picture_is_still_copied_and_the_wait_is_bounded(self, qapp_for_clipboard):
        output.copy_image_to_clipboard(make_image())

        assert self.timeouts == [output.WL_COPY_TIMEOUT_SECONDS]
        assert qapp_for_clipboard.clipboard().image().size() == make_image().size()

    def test_text_and_a_file_are_bounded_the_same_way(self, qapp_for_clipboard, tmp_path):
        output.copy_text_to_clipboard("hello")
        output.copy_file_to_clipboard(tmp_path / "clip.webm")

        assert self.timeouts == [output.WL_COPY_TIMEOUT_SECONDS] * 2
        assert [item.kind for item in output.copy_history()] == ["file", "text"]

    def test_it_says_what_that_means_for_the_copy(self, capsys):
        output.copy_text_to_clipboard("hello")

        said = capsys.readouterr().out
        assert "wl-copy" in said and "did not finish" in said
        assert "may not still be there after Snipux quits" in said

    def test_a_wl_copy_that_fails_says_so_too(self, monkeypatch, capsys):
        import subprocess

        def fails(argv, input=None, check=None, timeout=None):
            raise subprocess.CalledProcessError(1, argv)

        monkeypatch.setattr(output.subprocess, "run", fails)

        output.copy_text_to_clipboard("hello")

        assert "wl-copy" in capsys.readouterr().out


@pytest.fixture
def qapp_for_clipboard():
    from PyQt6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])
