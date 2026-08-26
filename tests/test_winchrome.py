"""Tests for `snipux/winchrome.py` -- the shared Settings/review chrome.

SNX-106: the title bar used to draw a plain accent-coloured square where the
app mark belongs. These cover `_title_bar_mark_pixmap()` picking the right
SNX-102 small-size artwork for the screen's pixel density, never reaching
for the detailed-master sizes, and `WinWindow` falling back to the old
square (rather than failing to open) when the artwork can't be loaded.
"""

from __future__ import annotations

import pytest
from PyQt6.QtGui import QColor, QImage, QPixmap
from PyQt6.QtWidgets import QApplication

from snipux import winchrome
from snipux.design import tokens


@pytest.fixture(scope="module", autouse=True)
def qapp():
    # PyQt6 needs a live QApplication to construct any QWidget/QPixmap, even
    # offscreen -- same convention as test_design.py/test_settings.py.
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


class _FakeScreen:
    """Stand-in for QScreen exposing only devicePixelRatio(), the one call
    `_title_bar_mark_pixmap()` makes -- matching test_capture.py's own
    `_FakeScreen`/`_FakeQGuiApplication` pattern for the same class.
    """

    def __init__(self, ratio: float):
        self._ratio = ratio

    def devicePixelRatio(self) -> float:
        return self._ratio


class _FakeQGuiApplication:
    def __init__(self, ratio: float | None):
        self._ratio = ratio

    def primaryScreen(self):
        return None if self._ratio is None else _FakeScreen(self._ratio)


def _solid_png(path, size: int, colour: QColor) -> None:
    image = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(colour)
    assert image.save(str(path), "PNG")


@pytest.fixture
def small_mark_dir(tmp_path, monkeypatch):
    # Three distinctly-coloured synthetic small-icon sizes, plus a fourth
    # "master downscale" size (48px, red) that must never be picked -- the
    # whole point of AC #3 ("not a fresh downscale of the detailed master").
    _solid_png(tmp_path / "snipux-16.png", 16, QColor(10, 10, 10))
    _solid_png(tmp_path / "snipux-24.png", 24, QColor(20, 20, 20))
    _solid_png(tmp_path / "snipux-32.png", 32, QColor(30, 30, 30))
    _solid_png(tmp_path / "snipux-48.png", 48, QColor(255, 0, 0))
    monkeypatch.setattr(winchrome, "_LOGO_DIR", tmp_path)
    return tmp_path


class TestTitleBarMarkPixmap:
    def test_picks_the_exact_small_size_at_1x(self, small_mark_dir, monkeypatch):
        monkeypatch.setattr(winchrome, "QGuiApplication", _FakeQGuiApplication(1.0))

        pixmap = winchrome._title_bar_mark_pixmap()

        assert not pixmap.isNull()
        assert pixmap.width() == winchrome._TITLEBAR_MARK_SIZE
        assert pixmap.devicePixelRatio() == pytest.approx(1.0)

    def test_picks_the_exact_small_size_at_2x(self, small_mark_dir, monkeypatch):
        # 16 logical px at 2x is 32 physical px -- exactly the largest small
        # size, so no scaling should be needed to reach it.
        monkeypatch.setattr(winchrome, "QGuiApplication", _FakeQGuiApplication(2.0))

        pixmap = winchrome._title_bar_mark_pixmap()

        assert not pixmap.isNull()
        assert pixmap.width() == 32
        assert pixmap.devicePixelRatio() == pytest.approx(2.0)
        # Logical size is what the label lays out with -- must stay 16, or
        # the mark would grow relative to the rest of the title bar.
        assert pixmap.width() / pixmap.devicePixelRatio() == pytest.approx(
            winchrome._TITLEBAR_MARK_SIZE
        )

    def test_never_reaches_past_the_small_sizes_even_at_high_scale(
        self, small_mark_dir, monkeypatch
    ):
        # 16 logical px at 3x needs 48 physical px, which only exists as the
        # (fake) master-downscale red square -- the largest *small* size
        # (32, dark grey) must be used and upscaled instead.
        monkeypatch.setattr(winchrome, "QGuiApplication", _FakeQGuiApplication(3.0))

        pixmap = winchrome._title_bar_mark_pixmap()

        assert not pixmap.isNull()
        image = pixmap.toImage()
        centre = image.pixelColor(image.width() // 2, image.height() // 2)
        assert centre != QColor(255, 0, 0)
        assert centre == QColor(30, 30, 30)

    def test_falls_back_to_the_largest_small_size_when_no_screen_is_reported(
        self, small_mark_dir, monkeypatch
    ):
        # QGuiApplication.primaryScreen() returning None is a documented
        # possibility (no screen attached yet); this must degrade to 1x
        # rather than raising.
        monkeypatch.setattr(winchrome, "QGuiApplication", _FakeQGuiApplication(None))

        pixmap = winchrome._title_bar_mark_pixmap()

        assert not pixmap.isNull()

    def test_missing_artwork_directory_returns_a_null_pixmap(self, tmp_path, monkeypatch):
        monkeypatch.setattr(winchrome, "_LOGO_DIR", tmp_path / "no-such-dir")
        monkeypatch.setattr(winchrome, "QGuiApplication", _FakeQGuiApplication(1.0))

        pixmap = winchrome._title_bar_mark_pixmap()

        assert pixmap.isNull()

    def test_unreadable_artwork_file_returns_a_null_pixmap(self, tmp_path, monkeypatch):
        (tmp_path / "snipux-16.png").write_bytes(b"not a real png")
        monkeypatch.setattr(winchrome, "_LOGO_DIR", tmp_path)
        monkeypatch.setattr(winchrome, "QGuiApplication", _FakeQGuiApplication(1.0))

        pixmap = winchrome._title_bar_mark_pixmap()

        assert pixmap.isNull()


class TestWinWindowTitleBarMark:
    def test_uses_the_real_vendored_artwork_by_default(self):
        # No monkeypatching -- exercises the actual shipped
        # design/logo/snipux-16.png the way the app really runs.
        window = winchrome.WinWindow("Settings", size=(400, 300))

        assert not window.title_mark.pixmap().isNull()

    def test_falls_back_to_the_accent_square_when_artwork_is_missing(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(winchrome, "_LOGO_DIR", tmp_path / "no-such-dir")

        window = winchrome.WinWindow("Settings", size=(400, 300))

        # The window must still open (no exception above) and show the old
        # plain square rather than a blank label.
        pixmap = window.title_mark.pixmap()
        assert pixmap is None or pixmap.isNull()
        assert tokens.Color.ACCENT in window.title_mark.styleSheet()
        assert window.title_mark.size().width() == 10

    def test_paints_without_error_using_the_real_artwork(self):
        # QWidget.grab() runs a full paintEvent offscreen -- CLAUDE.md's
        # preferred way to exercise painting code without a display.
        window = winchrome.WinWindow("Review", size=(400, 300))

        grabbed = window.grab()

        assert not grabbed.isNull()
