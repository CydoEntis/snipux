"""`history.py`: the panel of recent copies and saves."""

import datetime
from pathlib import Path

import pytest
from PyQt6.QtCore import QPoint, Qt
from PyQt6.QtGui import QImage
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication

from snipux import history, output


@pytest.fixture(scope="module", autouse=True)
def qapp():
    return QApplication.instance() or QApplication([])


def make_image(width=400, height=300, color=0xFF3366CC) -> QImage:
    image = QImage(width, height, QImage.Format.Format_RGB32)
    image.fill(color)
    return image


def copied(kind, value) -> output.CopiedItem:
    return output.CopiedItem(kind, value, datetime.datetime.now())


def save_png(path: Path, image: QImage | None = None) -> Path:
    (image or make_image()).save(str(path), "PNG")
    return path


class TestSavedThumbnail:
    def test_an_image_is_scaled_down_to_the_thumbnail_edge(self, tmp_path):
        path = save_png(tmp_path / "big.png", make_image(2000, 1000))

        thumb = history.saved_thumbnail(path)

        assert thumb.width() == history._THUMB_EDGE

    def test_a_recording_or_missing_file_has_none(self, tmp_path):
        video = tmp_path / "clip.mp4"
        video.write_bytes(b"not really a video")

        assert history.saved_thumbnail(video) is None
        assert history.saved_thumbnail(tmp_path / "gone.png") is None


class TestHistoryPanel:
    def test_opens_on_copied_with_one_card_per_copy(self):
        panel = history.HistoryPanel(
            [copied("image", make_image()), copied("text", "hello"),
             copied("file", Path("clip.webm"))],
            [],
        )

        assert panel.current_tab == "Copied"
        assert len(panel.cards) == 3
        assert panel.cards[1].preview.text() == "hello"
        assert panel.cards[0].preview.pixmap() is not None
        panel.grab()

    def test_the_saved_tab_lists_files_with_an_open_button(self, tmp_path):
        path = save_png(tmp_path / "snip.png")
        panel = history.HistoryPanel([], [path])

        panel.tab_buttons["Saved"].click()

        assert panel.current_tab == "Saved"
        assert len(panel.cards) == 1
        assert panel.cards[0].open_button is not None
        panel.grab()

    def test_an_empty_tab_paints_a_note_instead(self):
        panel = history.HistoryPanel([], [])

        assert panel.cards == []
        panel.grab()

    def test_clicking_a_card_asks_for_that_entry_to_be_copied(self):
        item = copied("text", "the one")
        panel = history.HistoryPanel([item], [])
        asked = []
        panel.copy_requested.connect(asked.append)
        panel.resize(history.PANEL_W, 600)
        panel.show()

        card = panel.cards[0]
        QTest.mouseClick(card, Qt.MouseButton.LeftButton, pos=QPoint(10, 10))
        panel.close()

        assert asked == [item]

    def test_open_asks_for_the_saved_file(self, tmp_path):
        path = save_png(tmp_path / "snip.png")
        panel = history.HistoryPanel([], [path])
        panel.show_tab("Saved")
        asked = []
        panel.open_requested.connect(asked.append)

        panel.cards[0].open_button.click()

        assert asked == [path]

    def test_refresh_keeps_the_tab_it_was_on(self, tmp_path):
        panel = history.HistoryPanel([], [])
        panel.show_tab("Saved")

        panel.refresh([], [save_png(tmp_path / "new.png")])

        assert panel.current_tab == "Saved"
        assert len(panel.cards) == 1

    def test_escape_closes_it(self):
        panel = history.HistoryPanel([], [])
        panel.show()

        QTest.keyClick(panel, Qt.Key.Key_Escape)

        assert not panel.isVisible()
