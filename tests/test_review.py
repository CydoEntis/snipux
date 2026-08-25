"""Tests for `snipux/review.py` -- the post-capture review window.

It is deliberately not an editor: what these assert is that it does the
things the overlay cannot do once it has closed, and nothing else.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PyQt6.QtGui import QImage
from PyQt6.QtWidgets import QApplication

from snipux.review import ReviewWindow


@pytest.fixture(scope="module", autouse=True)
def qapp():
    # PyQt6 needs a live QApplication to construct any QWidget, even
    # offscreen. Module-scoped, same as test_overlay.py's own.
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def make_image(width=400, height=300) -> QImage:
    image = QImage(width, height, QImage.Format.Format_RGB32)
    image.fill(0x336699)
    return image


class TestReviewWindow:
    def test_titles_itself_with_the_snips_real_size(self):
        window = ReviewWindow(make_image(640, 480))

        assert "640" in window.windowTitle() and "480" in window.windowTitle()

    def test_a_saved_snip_says_where_it_went(self, tmp_path):
        path = tmp_path / "shot.png"

        window = ReviewWindow(make_image(), saved_path=path)

        assert str(path) in window._status.text() or "shot.png" in window._status.text()

    def test_a_copied_snip_says_it_is_not_on_disk(self):
        # The distinction that matters: a copied snip is one clipboard write
        # away from being gone, and the window should say so.
        window = ReviewWindow(make_image())

        assert "not saved" in window._status.text().lower()

    def test_show_in_folder_is_disabled_for_a_snip_that_was_never_saved(self):
        window = ReviewWindow(make_image())

        assert not window._folder_button.isEnabled()

    def test_show_in_folder_is_enabled_once_there_is_a_file(self, tmp_path):
        window = ReviewWindow(make_image(), saved_path=tmp_path / "shot.png")

        assert window._folder_button.isEnabled()

    def test_save_as_writes_a_real_png(self, tmp_path):
        window = ReviewWindow(make_image(120, 90))
        target = tmp_path / "chosen.png"

        written = window.save_as(target)

        assert written == target
        assert target.exists()
        reloaded = QImage(str(target))
        assert (reloaded.width(), reloaded.height()) == (120, 90)

    def test_save_as_adds_a_png_extension_when_none_was_typed(self, tmp_path):
        window = ReviewWindow(make_image())

        written = window.save_as(tmp_path / "no-extension")

        assert written == tmp_path / "no-extension.png"
        assert written.exists()

    def test_save_as_creates_a_missing_directory(self, tmp_path):
        window = ReviewWindow(make_image())
        target = tmp_path / "not" / "there" / "shot.png"

        assert window.save_as(target) == target
        assert target.exists()

    def test_saving_enables_show_in_folder_for_a_copied_snip(self, tmp_path):
        window = ReviewWindow(make_image())
        assert not window._folder_button.isEnabled()

        window.save_as(tmp_path / "shot.png")

        assert window._folder_button.isEnabled()
        assert "shot.png" in window._status.text()

    def test_copy_puts_the_image_back_on_the_clipboard(self):
        # Worth having even for a snip that was copied on the way here:
        # anything copied since has replaced it.
        window = ReviewWindow(make_image(64, 64))

        window.copy()

        clipboard = QApplication.clipboard()
        assert clipboard.image().width() == 64
        assert "Copied" in window._status.text()

    def test_a_large_snip_is_previewed_scaled_down(self):
        window = ReviewWindow(make_image(4000, 3000))

        preview = window._preview.pixmap()

        assert preview.width() <= 960 and preview.height() <= 600

    def test_a_small_snip_is_previewed_at_its_true_size(self):
        # Only ever scaled down -- blowing a small snip up would show the
        # user something blurrier than what they captured.
        window = ReviewWindow(make_image(120, 90))

        preview = window._preview.pixmap()

        assert (preview.width(), preview.height()) == (120, 90)

    def test_paths_under_home_are_shown_relative_to_it(self):
        path = Path.home() / "Pictures" / "snipux" / "shot.png"

        assert ReviewWindow._display_path(path) == "~/Pictures/snipux/shot.png"

    def test_paths_outside_home_are_shown_in_full(self):
        assert ReviewWindow._display_path(Path("/tmp/shot.png")) == "/tmp/shot.png"

    def test_carries_no_annotation_tools(self):
        # The design constraint, asserted rather than trusted to review: a
        # second set of drawing tools would be a second implementation to
        # drift from the overlay's.
        window = ReviewWindow(make_image())

        labels = {b.text() for b in window.findChildren(type(window._copy_button))}
        assert labels == {"Copy", "Save As...", "Show in Folder", "Close"}
