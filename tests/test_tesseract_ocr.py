"""Linux text recognition, driven through a fake process call.

Runs on every OS: nothing in the module is Linux-only but the binary it
starts, and that is replaced here. No test starts a real tesseract.
"""

from __future__ import annotations

import os
import subprocess

import pytest
from PyQt6.QtCore import QRectF
from PyQt6.QtGui import QImage
from PyQt6.QtWidgets import QApplication

from snipux import sensitive
from snipux.platform import tesseract_ocr
from snipux.sensitive import RecognitionFailed

BINARY = "/usr/bin/tesseract"

LANGS = b'List of available languages in "/usr/share/tessdata/" (2):\neng\nosd\n'

HEADER = "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"


@pytest.fixture(scope="module", autouse=True)
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


@pytest.fixture(autouse=True)
def installed(monkeypatch):
    """A machine with tesseract, and with no package manager to name."""
    monkeypatch.setattr(tesseract_ocr, "find_binary", lambda: BINARY)
    monkeypatch.setattr(tesseract_ocr, "_manager", None)
    monkeypatch.setenv("LANG", "en_US.UTF-8")
    monkeypatch.delenv("LC_ALL", raising=False)
    monkeypatch.delenv("LC_MESSAGES", raising=False)


def image(width=2400, height=300) -> QImage:
    # Wider than UPSCALE_LONGEST_EDGE and shorter than two bands, so it is
    # read once, whole, at its own size unless a test asks otherwise.
    result = QImage(width, height, QImage.Format.Format_RGB32)
    result.fill(0xFFFFFF)
    return result


def word(line, number, left, top, width, height, text, *, block=1, par=1, conf="91.5") -> str:
    return f"5\t1\t{block}\t{par}\t{line}\t{number}\t{left}\t{top}\t{width}\t{height}\t{conf}\t{text}\n"


def tsv(*rows: str) -> bytes:
    return (HEADER + "".join(rows)).encode("utf-8")


class FakeRunner:
    """Stands in for `tesseract_ocr._run`: answers `--list-langs`, then
    hands each band's command the next of `outputs` (or the only one)."""

    def __init__(self, *outputs: bytes, langs=LANGS, code=0, langs_code=0, raises=None):
        self.outputs = list(outputs) or [tsv()]
        self.langs, self.code, self.langs_code, self.raises = langs, code, langs_code, raises
        self.commands: list[list[str]] = []
        self.timeouts: list[float] = []
        self.sizes_sent: list[tuple[int, int]] = []
        self.paths: list[str] = []

    def __call__(self, commands, timeout):
        self.timeouts.append(timeout)
        if commands == [[BINARY, "--list-langs"]]:
            return [(self.langs_code, self.langs)]
        if self.raises:
            raise self.raises
        results = []
        for index, command in enumerate(commands):
            self.commands.append(command)
            self.paths.append(command[1])
            sent = QImage(command[1])
            self.sizes_sent.append((sent.width(), sent.height()))
            output = self.outputs[index] if len(self.outputs) > 1 else self.outputs[0]
            results.append((self.code, output))
        return results


def texts(lines) -> list[list[str]]:
    return [[w.text for w in line] for line in lines]


class TestParsing:
    def test_words_are_grouped_into_their_lines(self):
        runner = FakeRunner(tsv(
            "1\t1\t0\t0\t0\t0\t0\t0\t2400\t300\t-1\t\n",
            "4\t1\t1\t1\t1\t0\t10\t20\t216\t14\t-1\t\n",
            word(1, 1, 10, 20, 60, 14, "Email:"),
            word(1, 2, 76, 20, 150, 14, "bob@gmail.com"),
            word(2, 1, 10, 50, 60, 14, "Phone:"),
        ))

        lines = tesseract_ocr.recognize(image(), runner=runner, processors=1)

        assert texts(lines) == [["Email:", "bob@gmail.com"], ["Phone:"]]
        assert lines[0][1].image_rect == QRectF(76, 20, 150, 14)

    def test_the_same_line_number_in_another_block_is_another_line(self):
        runner = FakeRunner(tsv(
            word(1, 1, 10, 20, 60, 14, "left", block=1),
            word(1, 1, 900, 20, 60, 14, "right", block=2),
        ))

        lines = tesseract_ocr.recognize(image(), runner=runner, processors=1)

        assert texts(lines) == [["left"], ["right"]]

    def test_lines_come_back_top_to_bottom_whatever_order_they_were_found_in(self):
        # Sparse-text mode promises no reading order.
        runner = FakeRunner(tsv(
            word(1, 1, 10, 200, 60, 14, "lower", block=1),
            word(1, 1, 10, 20, 60, 14, "upper", block=2),
        ))

        lines = tesseract_ocr.recognize(image(), runner=runner, processors=1)

        assert texts(lines) == [["upper"], ["lower"]]

    def test_words_in_a_line_are_in_reading_order(self):
        runner = FakeRunner(tsv(
            word(1, 2, 80, 20, 60, 14, "second"),
            word(1, 1, 10, 20, 60, 14, "first"),
        ))

        assert texts(tesseract_ocr.recognize(image(), runner=runner, processors=1)) == [
            ["first", "second"]
        ]

    def test_non_ascii_text_survives(self):
        runner = FakeRunner(tsv(word(1, 1, 1, 2, 3, 4, "zoë@exämple.com")))

        [[found]] = tesseract_ocr.recognize(image(), runner=runner, processors=1)

        assert found.text == "zoë@exämple.com"

    def test_rows_that_are_not_words_or_do_not_parse_are_skipped_not_fatal(self):
        runner = FakeRunner(tsv(
            "garbage\n",
            "5\t1\t1\t1\t1\t1\tx\t2\t3\t4\t90\tbad\n",
            word(1, 2, 1, 2, 3, 4, "   "),
            word(1, 3, 1, 2, 0, 4, "no-width"),
            word(1, 4, 1, 2, 3, 4, "good"),
        ))

        assert texts(tesseract_ocr.recognize(image(), runner=runner, processors=1)) == [["good"]]

    def test_a_word_tesseract_was_unsure_of_is_still_a_word(self):
        # A long key often comes back at confidence 0; it is still there.
        runner = FakeRunner(tsv(word(1, 1, 1, 2, 300, 14, "sk-proj-9fK2mQx7", conf="0.000000")))

        assert texts(tesseract_ocr.recognize(image(), runner=runner, processors=1)) == [
            ["sk-proj-9fK2mQx7"]
        ]

    def test_what_comes_back_is_what_hide_sensitive_reads(self):
        runner = FakeRunner(tsv(
            word(1, 1, 10, 20, 70, 14, "Password:"),
            word(1, 2, 90, 20, 150, 14, "hunter2-Correct!Horse"),
        ))

        lines = tesseract_ocr.recognize(image(), runner=runner, processors=1)
        [finding] = sensitive.find_sensitive(lines)

        assert finding.image_rect == QRectF(90, 20, 150, 14)


class TestScaling:
    def test_a_small_image_is_sent_doubled_and_its_boxes_halved_back(self):
        runner = FakeRunner(tsv(word(1, 1, 100, 40, 200, 28, "word")))

        [[found]] = tesseract_ocr.recognize(image(800, 150), runner=runner, processors=1)

        assert runner.sizes_sent == [(1600, 300)]
        assert found.image_rect == QRectF(50, 20, 100, 14)

    def test_a_large_image_is_sent_as_it_is(self):
        runner = FakeRunner()

        tesseract_ocr.recognize(image(2400, 300), runner=runner, processors=1)

        assert runner.sizes_sent == [(2400, 300)]

    def test_an_image_too_large_for_the_engine_is_shrunk(self):
        runner = FakeRunner(tsv(word(1, 1, 100, 10, 200, 20, "word")))

        [[found]] = tesseract_ocr.recognize(image(20000, 100), runner=runner, processors=1)

        assert runner.sizes_sent == [(10000, 50)]
        assert found.image_rect == QRectF(200, 20, 400, 40)


class TestBands:
    def test_a_tall_image_is_cut_into_overlapping_bands_read_together(self):
        runner = FakeRunner()

        tesseract_ocr.recognize(image(2400, 1200), runner=runner, processors=3)

        overlap = tesseract_ocr.BAND_OVERLAP
        assert runner.sizes_sent == [
            (2400, 400 + overlap), (2400, 400 + 2 * overlap), (2400, 400 + overlap)
        ]
        # One call for the languages, then one for every band at once.
        assert len(runner.timeouts) == 2

    def test_never_more_bands_than_processors(self):
        runner = FakeRunner()

        tesseract_ocr.recognize(image(2400, 2400), runner=runner, processors=2)

        assert len(runner.commands) == 2

    def test_a_short_image_is_not_cut_however_many_processors(self):
        runner = FakeRunner()

        tesseract_ocr.recognize(image(2400, 300), runner=runner, processors=16)

        assert runner.sizes_sent == [(2400, 300)]

    def test_a_word_is_put_back_where_its_band_was_cut_from(self):
        overlap = tesseract_ocr.BAND_OVERLAP
        second_band_top = 400 - overlap
        runner = FakeRunner(
            tsv(),
            tsv(word(1, 1, 30, 100, 80, 16, "middle")),
            tsv(),
        )

        [[found]] = tesseract_ocr.recognize(image(2400, 1200), runner=runner, processors=3)

        assert found.image_rect == QRectF(30, second_band_top + 100, 80, 16)

    def test_a_line_both_bands_can_see_comes_back_once(self):
        # The line sits at y 404..420 of the image: inside the first band's
        # overlap and the second band's own rows.
        overlap = tesseract_ocr.BAND_OVERLAP
        runner = FakeRunner(
            tsv(word(1, 1, 30, 404, 80, 16, "seam")),
            tsv(word(1, 1, 30, 404 - (400 - overlap), 80, 16, "seam")),
            tsv(),
        )

        lines = tesseract_ocr.recognize(image(2400, 1200), runner=runner, processors=3)

        assert texts(lines) == [["seam"]]
        assert lines[0][0].image_rect == QRectF(30, 404, 80, 16)

    def test_the_half_of_a_line_a_band_edge_cut_off_is_dropped(self):
        # The first band ends at 400 + overlap and sees only the top of a
        # line whose middle is the second band's.
        overlap = tesseract_ocr.BAND_OVERLAP
        edge = 400 + overlap
        runner = FakeRunner(
            tsv(word(1, 1, 30, edge - 6, 80, 6, "rnangled")),
            tsv(word(1, 1, 30, edge - 6 - (400 - overlap), 80, 16, "mangled")),
            tsv(),
        )

        assert texts(
            tesseract_ocr.recognize(image(2400, 1200), runner=runner, processors=3)
        ) == [["mangled"]]

    def test_bands_of_a_doubled_image_overlap_by_twice_as_much(self):
        runner = FakeRunner()

        tesseract_ocr.recognize(image(800, 600), runner=runner, processors=2)

        overlap = 2 * tesseract_ocr.BAND_OVERLAP
        assert runner.sizes_sent == [(1600, 600 + overlap), (1600, 600 + overlap)]


class TestTheCommand:
    def test_it_asks_for_tsv_in_english(self):
        runner = FakeRunner()

        tesseract_ocr.recognize(image(), runner=runner, processors=1)

        [command] = runner.commands
        assert command[0] == BINARY
        assert command[2:] == ["stdout", "-l", "eng", "--psm", "11", "tsv"]

    def test_the_temporary_files_are_gone_afterwards(self):
        runner = FakeRunner()

        tesseract_ocr.recognize(image(2400, 1200), runner=runner, processors=3)

        assert len(runner.paths) == 3
        assert not any(os.path.exists(path) for path in runner.paths)
        assert not os.path.exists(os.path.dirname(runner.paths[0]))

    def test_the_temporary_files_are_gone_after_a_failure_too(self):
        runner = FakeRunner(code=1)

        with pytest.raises(RecognitionFailed):
            tesseract_ocr.recognize(image(), runner=runner, processors=1)

        assert not os.path.exists(os.path.dirname(runner.paths[0]))


class TestLanguages:
    def pick(self, installed, locale):
        return tesseract_ocr._languages_to_read(installed, locale)

    def test_english_by_default(self):
        assert self.pick(["eng", "deu", "fra"], "en_US.UTF-8") == ["eng"]

    def test_the_users_own_language_first_and_english_with_it(self):
        assert self.pick(["eng", "deu", "fra"], "de_DE.UTF-8") == ["deu", "eng"]

    def test_a_language_not_installed_is_not_asked_for(self):
        assert self.pick(["eng"], "de_DE.UTF-8") == ["eng"]

    def test_a_language_whose_code_is_not_its_locale_spelled_out(self):
        assert self.pick(["eng", "pol"], "pl_PL.UTF-8") == ["pol", "eng"]

    def test_chinese_is_told_apart_by_region(self):
        assert self.pick(["chi_sim", "chi_tra", "eng"], "zh_TW.UTF-8") == ["chi_tra", "eng"]

    def test_never_every_language_installed(self):
        assert self.pick(["afr", "deu", "eng", "fra", "spa"], "C") == ["eng"]

    def test_without_english_whatever_is_there(self):
        assert self.pick(["fra", "deu"], "en_US.UTF-8") == ["deu"]

    def test_the_languages_installed_are_asked_for_on_every_read(self, monkeypatch):
        runner = FakeRunner(langs=b"List of available languages (3):\ndeu\neng\nosd\n")
        monkeypatch.setenv("LANG", "de_DE.UTF-8")

        tesseract_ocr.recognize(image(), runner=runner, processors=1)

        assert runner.commands[0][3:5] == ["-l", "deu+eng"]

    def test_orientation_and_equation_data_are_not_languages(self):
        runner = FakeRunner(langs=b"List of available languages (2):\nosd\nequ\n")

        with pytest.raises(RecognitionFailed, match="no language data installed"):
            tesseract_ocr.recognize(image(), runner=runner, processors=1)

        assert runner.commands == []


class TestNothingToRead:
    def test_a_page_with_no_words_is_no_lines(self):
        assert tesseract_ocr.recognize(image(), runner=FakeRunner(), processors=1) == []

    def test_a_null_image_never_starts_a_process(self):
        runner = FakeRunner()

        assert tesseract_ocr.recognize(QImage(), runner=runner) == []
        assert runner.timeouts == []


class TestAFailureIsNotAnEmptyPage:
    """The rule Windows learned the hard way: "could not read the picture"
    must never come back as no words, which is what "nothing sensitive
    here" looks like."""

    def test_running_out_of_time_says_so(self):
        runner = FakeRunner(raises=subprocess.TimeoutExpired("tesseract", 5))

        with pytest.raises(RecognitionFailed, match="took longer than 5 seconds"):
            tesseract_ocr.recognize(image(), runner=runner, processors=1)

    def test_the_default_wait_is_the_one_windows_gives(self):
        from snipux.platform import windows_ocr

        assert tesseract_ocr.TIMEOUT_SECONDS == windows_ocr.TIMEOUT_SECONDS == 5.0

    def test_the_bands_get_what_is_left_of_the_time_not_a_fresh_wait(self):
        runner = FakeRunner()

        tesseract_ocr.recognize(image(), runner=runner, processors=1, timeout=3.0)

        listing, reading = runner.timeouts
        assert listing == 3.0
        assert 0 < reading <= 3.0

    def test_a_tesseract_that_cannot_start_says_so(self):
        runner = FakeRunner(raises=OSError("Exec format error"))

        with pytest.raises(RecognitionFailed, match="could not start"):
            tesseract_ocr.recognize(image(), runner=runner, processors=1)

    def test_one_failed_band_fails_the_read(self):
        class OneBad(FakeRunner):
            def __call__(self, commands, timeout):
                results = super().__call__(commands, timeout)
                if len(results) == 3:
                    results[1] = (1, b"")
                return results

        with pytest.raises(RecognitionFailed, match="tesseract exit code 1"):
            tesseract_ocr.recognize(image(2400, 1200), runner=OneBad(), processors=3)

    def test_a_listing_that_fails_fails_the_read(self):
        runner = FakeRunner(langs_code=1)

        with pytest.raises(RecognitionFailed, match="tesseract exit code 1"):
            tesseract_ocr.recognize(image(), runner=runner, processors=1)

    def test_no_language_data_names_the_package_to_install(self, monkeypatch):
        monkeypatch.setattr(tesseract_ocr, "_manager", "pacman")
        runner = FakeRunner(langs=b"List of available languages (1):\nosd\n")

        with pytest.raises(RecognitionFailed) as failure:
            tesseract_ocr.recognize(image(), runner=runner, processors=1)

        assert str(failure.value) == (
            "tesseract has no language data installed (sudo pacman -S tesseract-data-eng)"
        )

    def test_uninstalled_between_the_overlay_opening_and_the_read(self, monkeypatch):
        monkeypatch.setattr(tesseract_ocr, "find_binary", lambda: None)
        runner = FakeRunner()

        with pytest.raises(RecognitionFailed, match="not installed"):
            tesseract_ocr.recognize(image(), runner=runner)

        assert runner.timeouts == []


class TestAvailability:
    def test_available_where_there_is_a_binary(self):
        assert tesseract_ocr.available() is True

    def test_not_available_where_there_is_none(self, monkeypatch):
        monkeypatch.setattr(tesseract_ocr, "find_binary", lambda: None)

        assert tesseract_ocr.available() is False

    @pytest.mark.parametrize(
        "manager, hint",
        [
            ("pacman", "Needs tesseract: sudo pacman -S tesseract tesseract-data-eng"),
            ("apt", "Needs tesseract: sudo apt install tesseract-ocr"),
            ("dnf", "Needs tesseract: sudo dnf install tesseract"),
        ],
    )
    def test_the_hint_is_the_command_for_this_distribution(self, monkeypatch, manager, hint):
        looked_for = []

        def which(name):
            looked_for.append(name)
            return f"/usr/bin/{name}" if name == manager else None

        monkeypatch.setattr(tesseract_ocr.shutil, "which", which)
        tesseract_ocr.reset()

        assert tesseract_ocr.install_hint() == hint
        # Asked on every overlay open, so the search is made once.
        searches = len(looked_for)
        assert tesseract_ocr.install_hint() == hint
        assert len(looked_for) == searches

    def test_the_hint_still_names_tesseract_on_a_distribution_we_do_not_know(self):
        assert "tesseract" in tesseract_ocr.install_hint()


class TestTheRealRunner:
    """`_run` itself, against this Python instead of tesseract."""

    def script(self, body: str) -> list[str]:
        import sys

        return [sys.executable, "-c", body]

    def test_every_command_answers_in_order(self):
        results = tesseract_ocr._run(
            [self.script("print('one')"), self.script("import sys; print('two'); sys.exit(3)")], 30
        )

        assert [(code, out.strip()) for code, out in results] == [(0, b"one"), (3, b"two")]

    def test_more_output_than_a_pipe_holds_does_not_block(self):
        [(code, out)] = tesseract_ocr._run([self.script("print('x' * 300000)")], 30)

        assert code == 0
        assert len(out.strip()) == 300000

    def test_each_process_is_held_to_one_thread(self):
        [(_code, out)] = tesseract_ocr._run(
            [self.script("import os; print(os.environ.get('OMP_THREAD_LIMIT'))")], 30
        )

        assert out.strip() == b"1"

    def test_out_of_time_every_process_is_killed(self, tmp_path):
        marker = tmp_path / "survived"
        slow = self.script(
            f"import time, pathlib; time.sleep(1.5); pathlib.Path({str(marker)!r}).write_text('x')"
        )

        with pytest.raises(subprocess.TimeoutExpired):
            tesseract_ocr._run([slow, slow], 0.3)

        import time

        time.sleep(1.8)
        assert not marker.exists()
