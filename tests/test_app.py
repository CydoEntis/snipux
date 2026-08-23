import re

import pytest
from PyQt6.QtGui import QGuiApplication, QImage, qRgb
from PyQt6.QtWidgets import QApplication

from snipux import app
from snipux.app import build_default_registry, copy_image_to_clipboard, main, save_image
from snipux.capture import BackendRegistry, CaptureBackend

FILL_COLOR = qRgb(10, 20, 30)


@pytest.fixture(scope="module", autouse=True)
def qapp():
    # QGuiApplication.clipboard() needs a live application instance even
    # when no widget is ever created, and this file must not depend on
    # test_editor.py having already created one — module-scoped like
    # test_editor.py's own fixture, but independent of it.
    instance = QApplication.instance()
    if instance is None:
        instance = QApplication([])
    return instance


def make_image(size=(20, 10), fill_color=FILL_COLOR) -> QImage:
    image = QImage(*size, QImage.Format.Format_RGB32)
    image.fill(fill_color)
    return image


class FakeBackend(CaptureBackend):
    def __init__(self, backend_name, available, reason=None):
        self._name = backend_name
        self._available = available
        self._reason = reason

    def name(self):
        return self._name

    def is_available(self):
        return self._available

    def unavailable_reason(self):
        return self._reason

    def capture(self):
        raise NotImplementedError


def test_list_backends_reports_name_availability_and_reason(capsys):
    registry = BackendRegistry(
        [
            FakeBackend("qt-native", True),
            FakeBackend("grim", False, reason="not on Wayland"),
        ]
    )

    exit_code = main(["--list-backends"], registry=registry)

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "qt-native" in out
    assert "available" in out
    assert "grim" in out
    assert "not on Wayland" in out


def test_list_backends_on_empty_registry_reports_none_registered(capsys):
    registry = BackendRegistry()

    exit_code = main(["--list-backends"], registry=registry)

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "no backends registered" in out


def test_no_arguments_prints_usage_mentioning_list_backends(capsys):
    registry = BackendRegistry()

    exit_code = main([], registry=registry)

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "--list-backends" in out


def test_build_default_registry_starts_empty():
    registry = build_default_registry()

    assert registry.available() == []


def test_main_does_not_require_a_display():
    # Guards against snipux.app accidentally importing something that
    # needs a live QApplication at import time; run under
    # QT_QPA_PLATFORM=offscreen like the rest of the suite.
    exit_code = main(["--list-backends"], registry=BackendRegistry())
    assert exit_code == 0


class TestCopyImageToClipboard:
    def test_always_places_the_image_on_the_qt_clipboard(self, monkeypatch):
        monkeypatch.setattr(app.shutil, "which", lambda binary: None)
        image = make_image()

        copy_image_to_clipboard(image)

        assert QGuiApplication.clipboard().image() == image

    def test_pipes_to_wl_copy_when_present_on_path(self, monkeypatch):
        monkeypatch.setattr(app.shutil, "which", lambda binary: f"/usr/bin/{binary}")
        calls = []

        def fake_run(argv, input=None, check=None):
            calls.append((argv, input))

        monkeypatch.setattr(app.subprocess, "run", fake_run)
        image = make_image(fill_color=qRgb(1, 2, 3))

        copy_image_to_clipboard(image)

        assert len(calls) == 1
        argv, piped_bytes = calls[0]
        assert argv == ["wl-copy", "--type", "image/png"]
        # Sample pixel colors rather than asserting whole-image equality:
        # QImage.__eq__ also compares format, and a PNG round trip isn't
        # guaranteed to hand back the exact same format as the RGB32
        # source on every Qt build. Matches the sampling convention used
        # throughout test_editor.py.
        round_tripped = QImage()
        assert round_tripped.loadFromData(piped_bytes)
        assert round_tripped.size() == image.size()
        assert round_tripped.pixelColor(0, 0) == image.pixelColor(0, 0)
        assert round_tripped.pixelColor(
            image.width() - 1, image.height() - 1
        ) == image.pixelColor(image.width() - 1, image.height() - 1)

    def test_does_not_raise_and_falls_back_to_qt_clipboard_when_wl_copy_absent(
        self, monkeypatch
    ):
        monkeypatch.setattr(app.shutil, "which", lambda binary: None)
        calls = []
        monkeypatch.setattr(
            app.subprocess, "run", lambda *a, **k: calls.append((a, k))
        )
        image = make_image(fill_color=qRgb(4, 5, 6))

        copy_image_to_clipboard(image)  # must not raise

        assert calls == []
        assert QGuiApplication.clipboard().image() == image

    def test_does_not_raise_when_wl_copy_binary_vanishes_before_running(self, monkeypatch):
        # A TOCTOU race: shutil.which found it, but the run itself fails.
        monkeypatch.setattr(app.shutil, "which", lambda binary: f"/usr/bin/{binary}")

        def raising_run(*args, **kwargs):
            raise FileNotFoundError("wl-copy")

        monkeypatch.setattr(app.subprocess, "run", raising_run)
        image = make_image()

        copy_image_to_clipboard(image)  # must not raise

        assert QGuiApplication.clipboard().image() == image


class TestSaveImage:
    def test_writes_into_the_given_directory_and_returns_the_path(self, tmp_path):
        image = make_image()

        path = save_image(image, tmp_path)

        assert path.parent == tmp_path
        assert path.exists()
        assert QImage(str(path)) == image

    def test_default_filename_derives_from_current_date_and_time(self, tmp_path):
        image = make_image()

        path = save_image(image, tmp_path)

        assert re.fullmatch(r"Screenshot from \d{4}-\d{2}-\d{2} \d{2}-\d{2}-\d{2}\.png", path.name)

    def test_default_directory_is_pictures_under_home_and_is_created(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setattr(app.Path, "home", lambda: tmp_path)
        image = make_image()

        path = save_image(image)

        assert path.parent == tmp_path / "Pictures"
        assert path.exists()
