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


class TestFramelessWindowsCanStillBeResized:
    """`FramelessWindowHint` takes the window manager's resize borders away
    with everything else it removes, and nothing replaced them: Settings,
    the review window and the player all opened at a fixed size and stayed
    there. These cover the borders `WinWindow` grows for itself.

    They drive `_handle_edge_event` directly and exercise the manual
    fallback, because `startSystemResize` needs a real compositor and the
    suite runs headless -- which is also the reason that fallback exists.
    """

    @staticmethod
    def _window(width=900, height=600):
        window = winchrome.WinWindow("Test", size=(width, height))
        window.show()
        window.setGeometry(100, 100, width, height)
        return window

    @staticmethod
    def _event(window, kind, global_x, global_y):
        from PyQt6.QtCore import QPoint, QPointF
        from PyQt6.QtGui import QMouseEvent
        from PyQt6.QtCore import Qt

        return QMouseEvent(
            kind,
            QPointF(window.mapFromGlobal(QPoint(global_x, global_y))),
            QPointF(global_x, global_y),
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        )

    def _drag(self, window, from_xy, to_xy):
        from PyQt6.QtCore import QEvent

        window._handle_edge_event(
            QEvent.Type.MouseButtonPress,
            self._event(window, QEvent.Type.MouseButtonPress, *from_xy),
        )
        window._handle_edge_event(
            QEvent.Type.MouseMove, self._event(window, QEvent.Type.MouseMove, *to_xy)
        )
        window._handle_edge_event(
            QEvent.Type.MouseButtonRelease,
            self._event(window, QEvent.Type.MouseButtonRelease, *to_xy),
        )

    def test_the_bottom_right_corner_grows_the_window(self):
        window = self._window()
        rect = window.geometry()

        self._drag(window, (rect.right() - 2, rect.bottom() - 2),
                   (rect.right() + 198, rect.bottom() + 148))

        assert window.geometry().width() == 1100
        assert window.geometry().height() == 750

    def test_the_left_edge_moves_rather_than_stretching_the_other_side(self):
        window = self._window()
        rect = window.geometry()

        self._drag(window, (rect.left() + 2, rect.center().y()),
                   (rect.left() + 102, rect.center().y()))

        assert window.geometry().left() == rect.left() + 100
        assert window.geometry().right() == rect.right()

    def test_a_window_cannot_be_collapsed_past_its_minimum(self):
        # Otherwise it is dragged down to a sliver it can never be got back
        # out of, because the grips go with it.
        window = self._window()
        rect = window.geometry()

        self._drag(window, (rect.right() - 2, rect.bottom() - 2),
                   (rect.left(), rect.top()))

        assert window.geometry().width() == window.minimumSize().width()
        assert window.geometry().height() == window.minimumSize().height()

    def test_the_middle_of_the_window_is_not_an_edge(self):
        from PyQt6.QtCore import QPoint

        window = self._window()

        assert not window._edges_at(QPoint(450, 300))
        assert window._edges_at(QPoint(1, 1))
        assert window._edges_at(QPoint(899, 599))

    def test_each_edge_gets_the_cursor_that_describes_it(self):
        from PyQt6.QtCore import QPoint, Qt

        window = self._window()
        shape = lambda x, y: window._cursor_for(window._edges_at(QPoint(x, y)))

        assert shape(2, 300) == Qt.CursorShape.SizeHorCursor
        assert shape(450, 2) == Qt.CursorShape.SizeVerCursor
        assert shape(2, 2) == Qt.CursorShape.SizeFDiagCursor
        assert shape(898, 2) == Qt.CursorShape.SizeBDiagCursor

    def test_the_title_bar_still_drags_the_window(self):
        # The resize border runs along the top of the title bar, so the
        # check that added it could easily have eaten the move gesture.
        from PyQt6.QtCore import QEvent

        window = self._window()
        window.mousePressEvent(self._event(window, QEvent.Type.MouseButtonPress, 400, 120))

        assert window._drag_origin is not None

    def test_the_title_bars_own_corner_resizes_rather_than_moves(self):
        from PyQt6.QtCore import QEvent

        window = self._window()
        rect = window.geometry()
        window.mousePressEvent(
            self._event(window, QEvent.Type.MouseButtonPress, rect.left() + 1, rect.top() + 1)
        )

        assert window._drag_origin is None

    def test_an_ordinary_click_inside_the_window_is_left_alone(self):
        # The filter runs on every descendant, so swallowing a non-resize
        # event here would break every button in the window.
        from PyQt6.QtCore import QEvent

        window = self._window()
        handled = window._handle_edge_event(
            QEvent.Type.MouseButtonPress,
            self._event(window, QEvent.Type.MouseButtonPress, 500, 400),
        )

        assert handled is False

    def test_a_maximised_window_has_no_resize_edges(self):
        from PyQt6.QtCore import QEvent

        window = self._window()
        window.showMaximized()
        rect = window.geometry()
        handled = window._handle_edge_event(
            QEvent.Type.MouseButtonPress,
            self._event(window, QEvent.Type.MouseButtonPress,
                        rect.right() - 2, rect.bottom() - 2),
        )

        assert handled is False

    def test_widgets_added_after_construction_are_watched_too(self):
        # Subclasses fill `body` well after __init__, so registering only
        # what exists at construction would leave the sides uncovered.
        from PyQt6.QtWidgets import QLabel

        window = self._window()
        late = QLabel("added later", window.body)

        # An installed event filter is not introspectable, but the mouse
        # tracking set alongside it is -- and without that a hover over the
        # edge never reaches the filter in the first place, so it is the
        # half worth asserting.
        assert late.hasMouseTracking()
        assert QLabel("deeper still", late).hasMouseTracking()
