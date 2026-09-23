"""SNX-83: Pin -- a snip kept on top of everything, exactly where it was
cut. `snipux/pin.py`'s own module docstring is the design authority.

Every test here grabs or drives the widget offscreen rather than calling
`show()`: `PinWindow.windowHandle()` stays `None` without a real native
window, which is exactly the condition under which dragging and resizing
must take the manual fallback rather than `QWindow.startSystemMove`/
`startSystemResize` -- the same "offscreen platform takes the fallback"
rule `winchrome.py`'s own resize borders already rely on for their tests.
"""

from __future__ import annotations

import pytest
from PyQt6.QtCore import QEvent, QPoint, QPointF, QRect, Qt
from PyQt6.QtGui import QGuiApplication, QImage, QMouseEvent
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication

from snipux import pin as pin_module
from snipux.pin import PinWindow


@pytest.fixture(scope="module", autouse=True)
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def make_image(width=400, height=300) -> QImage:
    image = QImage(width, height, QImage.Format.Format_RGB32)
    image.fill(0x336699)
    return image


def _mouse_event(kind, local: QPoint, glob: QPoint, *, button, buttons) -> QMouseEvent:
    return QMouseEvent(
        kind,
        QPointF(local),
        QPointF(glob),
        button,
        buttons,
        Qt.KeyboardModifier.NoModifier,
    )


class TestGeometry:
    def test_opens_at_exactly_the_selections_rect(self):
        rect = QRect(120, 84, 640, 480)
        window = PinWindow(make_image(640, 480), rect)

        assert window.geometry() == rect

    def test_a_tiny_selection_still_opens_at_its_own_size(self):
        # The minimum-size floor must never clamp construction itself --
        # see `PinWindow.__init__`'s own comment on this.
        rect = QRect(0, 0, 20, 15)
        window = PinWindow(make_image(20, 15), rect)

        assert window.geometry() == rect


class TestAlwaysOnTop:
    def test_stays_on_top_is_set(self):
        window = PinWindow(make_image(), QRect(0, 0, 400, 300))

        assert bool(window.windowFlags() & Qt.WindowType.WindowStaysOnTopHint)

    def test_frameless_with_no_title_bar_chrome(self):
        window = PinWindow(make_image(), QRect(0, 0, 400, 300))

        assert bool(window.windowFlags() & Qt.WindowType.FramelessWindowHint)


class TestEscapeCloses:
    def test_escape_closes_the_window(self):
        window = PinWindow(make_image(), QRect(0, 0, 400, 300))
        seen = []
        window.closed.connect(lambda: seen.append(True))

        QTest.keyClick(window, Qt.Key.Key_Escape)

        assert seen == [True]


class TestSeveralPinsCoexist:
    def test_closing_one_leaves_the_other_open(self):
        first = PinWindow(make_image(), QRect(0, 0, 200, 150))
        second = PinWindow(make_image(), QRect(300, 0, 200, 150))
        first_closed = []
        second_closed = []
        first.closed.connect(lambda: first_closed.append(True))
        second.closed.connect(lambda: second_closed.append(True))

        first.close()

        assert first_closed == [True]
        assert second_closed == []


class TestDragByTheImage:
    def test_dragging_moves_the_window_via_the_offscreen_fallback(self):
        window = PinWindow(make_image(200, 150), QRect(100, 100, 200, 150))
        assert window.windowHandle() is None  # never shown: no native window to ask

        # Well clear of the resize margin, so this presses the plain body.
        window.mousePressEvent(
            _mouse_event(
                QEvent.Type.MouseButtonPress,
                QPoint(100, 75),
                QPoint(200, 175),
                button=Qt.MouseButton.LeftButton,
                buttons=Qt.MouseButton.LeftButton,
            )
        )
        window.mouseMoveEvent(
            _mouse_event(
                QEvent.Type.MouseMove,
                QPoint(150, 100),
                QPoint(250, 200),
                button=Qt.MouseButton.NoButton,
                buttons=Qt.MouseButton.LeftButton,
            )
        )

        assert window.pos() == QPoint(150, 125)


class TestContextMenu:
    """The right-click menu's Copy/Save must not be a second, divergent
    implementation -- they call the same `copy()`/`save()` a test can call
    directly, so this proves the menu is wired to them rather than proving
    the export logic itself twice.
    """

    def test_the_menu_offers_copy_and_save(self):
        window = PinWindow(make_image(), QRect(0, 0, 400, 300))

        menu = window._build_context_menu()

        assert [action.text() for action in menu.actions()] == ["Copy", "Save"]

    def test_copy_action_puts_the_same_bytes_on_the_clipboard_as_copy_does(self):
        image = make_image(120, 90)
        window = PinWindow(image, QRect(0, 0, 120, 90))
        menu = window._build_context_menu()
        copy_action = next(a for a in menu.actions() if a.text() == "Copy")

        copy_action.trigger()
        via_menu = QGuiApplication.clipboard().image()

        QGuiApplication.clipboard().clear()
        window.copy()
        via_direct_call = QGuiApplication.clipboard().image()

        assert via_menu == image
        assert via_direct_call == image

    def test_save_action_writes_the_same_bytes_as_save_does(self, monkeypatch, tmp_path):
        monkeypatch.setattr(pin_module.Path, "home", lambda: tmp_path)
        image = make_image(120, 90)
        window = PinWindow(image, QRect(0, 0, 120, 90))
        menu = window._build_context_menu()
        save_action = next(a for a in menu.actions() if a.text() == "Save")

        save_action.trigger()
        via_menu = sorted((tmp_path / "Pictures" / "snipux").glob("*.png"))
        assert len(via_menu) == 1
        assert QImage(str(via_menu[0])) == image

        direct_path = window.save()

        assert QImage(str(direct_path)) == image


class TestSaveHonoursTheConfiguredFolder:
    """Pin's Save wrote to a hardcoded ~/Pictures/snipux, so a folder
    chosen in Settings applied to every Save in the app except this one --
    and a failed write raised `OSError` straight into the Qt event loop
    from a context-menu action, with the user told nothing.
    """

    def test_it_saves_where_settings_says(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            pin_module.setup_desktop, "load_save_folder", lambda cd=None: tmp_path
        )
        window = PinWindow(make_image(60, 40), QRect(0, 0, 60, 40))

        path = window.save()

        assert path is not None
        assert path.parent == tmp_path

    def test_a_failed_write_is_reported_not_raised(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            pin_module.setup_desktop, "load_save_folder", lambda cd=None: tmp_path
        )

        def refuse(_image, _directory=None):
            raise OSError("disk full")

        monkeypatch.setattr(pin_module.output, "save_image", refuse)
        said = []
        monkeypatch.setattr(
            pin_module.QToolTip, "showText", lambda _pos, text, _w=None: said.append(text)
        )
        window = PinWindow(make_image(60, 40), QRect(0, 0, 60, 40))

        assert window.save() is None
        assert said and "disk full" in said[0]
