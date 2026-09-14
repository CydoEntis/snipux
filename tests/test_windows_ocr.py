"""Windows text recognition, driven through a fake process call.

Runs on every OS: the only Windows-specific thing in the module is the
command it starts, and that is replaced here.
"""

from __future__ import annotations

import base64
import os
import re
import subprocess

import pytest
from PyQt6.QtCore import QRectF
from PyQt6.QtGui import QImage
from PyQt6.QtWidgets import QApplication

from snipux.platform import windows_ocr


@pytest.fixture(scope="module", autouse=True)
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def image(width=2400, height=1200) -> QImage:
    # Longer than UPSCALE_LONGEST_EDGE, so it is read once at its own size
    # unless a test asks otherwise.
    result = QImage(width, height, QImage.Format.Format_RGB32)
    result.fill(0xFFFFFF)
    return result


def script_of(command: list[str]) -> str:
    return base64.b64decode(command[-1]).decode("utf-16-le")


def paths_in(command: list[str]) -> list[str]:
    listed = re.search(r"\$paths = @\((.*)\)", script_of(command)).group(1)
    return [path.replace("''", "'") for path in re.findall(r"'((?:[^']|'')*)'", listed)]


class FakeRunner:
    def __init__(self, code=0, stdout=b"", raises=None):
        self.code, self.stdout, self.raises = code, stdout, raises
        self.commands: list[list[str]] = []
        self.paths: list[str] = []
        self.files_existed_during_run = None
        self.sizes_sent: list[tuple[int, int]] = []

    def __call__(self, command, timeout):
        self.commands.append(command)
        self.paths = paths_in(command)
        self.files_existed_during_run = all(os.path.exists(path) for path in self.paths)
        for path in self.paths:
            sent = QImage(path)
            self.sizes_sent.append((sent.width(), sent.height()))
        if self.raises:
            raise self.raises
        return self.code, self.stdout


class TestParsing:
    def test_words_are_grouped_into_their_lines(self):
        runner = FakeRunner(stdout=(
            "0\t0\t10\t20\t60\t14\tEmail:\n"
            "0\t0\t76\t20\t150\t14\tbob@gmail.com\n"
            "0\t1\t10\t50\t60\t14\tPhone:\n"
        ).encode("utf-8"))

        lines = windows_ocr.recognize(image(), runner=runner)

        assert [[w.text for w in line] for line in lines] == [["Email:", "bob@gmail.com"], ["Phone:"]]
        assert lines[0][1].image_rect == QRectF(76, 20, 150, 14)

    def test_non_ascii_text_survives(self):
        runner = FakeRunner(stdout="0\t0\t1\t2\t3\t4\tzoë@exämple.com\n".encode("utf-8"))

        [[word]] = windows_ocr.recognize(image(), runner=runner)

        assert word.text == "zoë@exämple.com"

    def test_a_utf8_byte_order_mark_is_not_part_of_the_first_field(self):
        runner = FakeRunner(stdout="\ufeff0\t0\t1\t2\t3\t4\tHello\n".encode("utf-8"))

        [[word]] = windows_ocr.recognize(image(), runner=runner)

        assert word.text == "Hello"

    def test_fractional_invariant_culture_numbers(self):
        runner = FakeRunner(stdout=b"0\t0\t10.5\t20.25\t30\t12.75\tword\n")

        [[word]] = windows_ocr.recognize(image(), runner=runner)

        assert word.image_rect == QRectF(10.5, 20.25, 30, 12.75)

    def test_a_malformed_line_is_skipped_not_fatal(self):
        runner = FakeRunner(stdout=b"garbage\n0\t0\tx\t1\t2\t3\tbad\n9\t0\t1\t2\t3\t4\tno-such-read\n0\t0\t1\t2\t3\t4\tgood\n")

        lines = windows_ocr.recognize(image(), runner=runner)

        assert [[w.text for w in line] for line in lines] == [["good"]]

    def test_a_tab_inside_the_text_field_is_kept(self):
        runner = FakeRunner(stdout=b"0\t0\t1\t2\t3\t4\ttwo\tparts\n")

        [[word]] = windows_ocr.recognize(image(), runner=runner)

        assert word.text == "two\tparts"


class TestFailuresReturnNothing:
    def test_empty_output(self):
        assert windows_ocr.recognize(image(), runner=FakeRunner(stdout=b"")) == []

    def test_non_zero_exit(self):
        runner = FakeRunner(code=1, stdout=b"0\t0\t1\t2\t3\t4\tignored\n")

        assert windows_ocr.recognize(image(), runner=runner) == []

    def test_timeout(self):
        runner = FakeRunner(raises=subprocess.TimeoutExpired("powershell.exe", 10))

        assert windows_ocr.recognize(image(), runner=runner) == []

    def test_powershell_missing(self):
        runner = FakeRunner(raises=FileNotFoundError("powershell.exe"))

        assert windows_ocr.recognize(image(), runner=runner) == []

    def test_a_null_image_never_starts_a_process(self):
        runner = FakeRunner()

        assert windows_ocr.recognize(QImage(), runner=runner) == []
        assert runner.commands == []


class TestTheTemporaryFiles:
    def test_they_exist_while_recognition_runs_and_are_gone_after(self):
        runner = FakeRunner(stdout=b"0\t0\t1\t2\t3\t4\tword\n")

        windows_ocr.recognize(image(800, 400), runner=runner)

        assert len(runner.paths) == 2
        assert runner.files_existed_during_run is True
        assert not any(os.path.exists(path) for path in runner.paths)

    def test_they_are_gone_after_a_failure_too(self):
        runner = FakeRunner(raises=subprocess.TimeoutExpired("powershell.exe", 10))

        windows_ocr.recognize(image(800, 400), runner=runner)

        assert not any(os.path.exists(path) for path in runner.paths)

    def test_the_paths_handed_to_windows_are_absolute(self):
        # StorageFile.GetFileFromPathAsync rejects relative paths.
        runner = FakeRunner()

        windows_ocr.recognize(image(800, 400), runner=runner)

        assert all(os.path.isabs(path) for path in runner.paths)


class TestTheCommand:
    def test_runs_powershell_once_without_a_profile_or_prompts(self):
        runner = FakeRunner()

        windows_ocr.recognize(image(800, 400), runner=runner)

        [command] = runner.commands
        assert command[0] == "powershell.exe"
        assert "-NoProfile" in command and "-NonInteractive" in command
        assert command[-2] == "-EncodedCommand"

    def test_the_script_asks_for_utf8_output(self):
        # PowerShell 5.1 otherwise writes redirected output in the console's
        # OEM code page, which breaks any accented character.
        runner = FakeRunner()

        windows_ocr.recognize(image(), runner=runner)

        assert "OutputEncoding = [Text.Encoding]::UTF8" in script_of(runner.commands[0])

    def test_a_quote_in_a_path_is_escaped_for_powershell(self):
        command = windows_ocr._command_for([r"C:\Users\O'Brien\snipux-ocr-1.bmp"])

        assert r"'C:\Users\O''Brien\snipux-ocr-1.bmp'" in script_of(command)
        assert paths_in(command) == [r"C:\Users\O'Brien\snipux-ocr-1.bmp"]


class TestReadingAtTwoSizes:
    def test_a_small_image_is_read_at_its_own_size_and_doubled(self):
        runner = FakeRunner()

        windows_ocr.recognize(image(800, 400), runner=runner)

        assert runner.sizes_sent == [(800, 400), (1600, 800)]

    def test_each_reads_rects_come_back_in_original_pixels(self):
        runner = FakeRunner(stdout=(
            "0\t0\t50\t20\t30\t14\tfrom-the-first-read\n"
            "1\t0\t100\t40\t60\t28\tfrom-the-doubled-read\n"
        ).encode("utf-8"))

        first, doubled = windows_ocr.recognize(image(800, 400), runner=runner)

        assert first[0].image_rect == QRectF(50, 20, 30, 14)
        assert doubled[0].image_rect == QRectF(50, 20, 30, 14)

    def test_lines_from_the_two_reads_are_never_joined(self):
        runner = FakeRunner(stdout=(
            "0\t0\t1\t1\t1\t1\tAPI\n"
            "1\t0\t1\t1\t1\t1\tAPI\n"
            "1\t0\t2\t1\t1\t1\tKEY=abc\n"
        ).encode("utf-8"))

        lines = windows_ocr.recognize(image(800, 400), runner=runner)

        assert [[w.text for w in line] for line in lines] == [["API"], ["API", "KEY=abc"]]

    def test_a_large_image_is_read_once(self):
        runner = FakeRunner()

        windows_ocr.recognize(image(2400, 1200), runner=runner)

        assert runner.sizes_sent == [(2400, 1200)]

    def test_images_over_the_engine_limit_are_shrunk_to_fit(self):
        assert windows_ocr._scales_for(20000, 5000) == [pytest.approx(0.5)]

    def test_mid_sized_images_are_read_once_as_they_are(self):
        assert windows_ocr._scales_for(5120, 2880) == [1.0]


class TestAvailability:
    def test_needs_windows(self, monkeypatch):
        monkeypatch.setattr(windows_ocr.sys, "platform", "linux")
        monkeypatch.setattr(windows_ocr.shutil, "which", lambda name: "powershell.exe")

        assert windows_ocr.available() is False

    def test_needs_powershell(self, monkeypatch):
        monkeypatch.setattr(windows_ocr.sys, "platform", "win32")
        monkeypatch.setattr(windows_ocr.shutil, "which", lambda name: None)

        assert windows_ocr.available() is False

    def test_available_with_both(self, monkeypatch):
        monkeypatch.setattr(windows_ocr.sys, "platform", "win32")
        monkeypatch.setattr(windows_ocr.shutil, "which", lambda name: r"C:\Windows\powershell.exe")

        assert windows_ocr.available() is True
