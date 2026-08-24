import pytest
from PyQt6.QtGui import QColor, QIcon
from PyQt6.QtWidgets import QApplication

import snipux.design as design
from snipux.design import tokens

SOLID_ICON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="24" height="24">'
    '<rect width="24" height="24" fill="currentColor"/></svg>'
)


@pytest.fixture(scope="module", autouse=True)
def qapp():
    # QPixmap/QPainter/QSvgRenderer (icon()) and QFontDatabase (font_families())
    # all need a live QApplication, even offscreen, matching the convention in
    # test_capture.py/test_editor.py/test_overlay.py.
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


@pytest.fixture
def solid_icon_dir(tmp_path, monkeypatch):
    # A synthetic icon that fills its whole 24x24 box with currentColor,
    # rather than one of the real stroke-only glyphs, so a recolour test can
    # assert an exact pixel colour without depending on where a 1.55px
    # stroke happens to land at this render size.
    (tmp_path / "swatch.svg").write_text(SOLID_ICON_SVG, encoding="utf-8")
    monkeypatch.setattr(design, "_ICON_DIR", tmp_path)
    return tmp_path


class TestIcon:
    def test_recolours_the_glyph_to_the_requested_colour(self, solid_icon_dir):
        result = design.icon("swatch", QColor(255, 0, 0))

        assert isinstance(result, QIcon)
        pixmap = result.pixmap(24, 24)
        image = pixmap.toImage()
        center = image.pixelColor(12, 12)
        assert (center.red(), center.green(), center.blue(), center.alpha()) == (
            255,
            0,
            0,
            255,
        )

    def test_accepts_a_hex_string_same_as_a_qcolor(self, solid_icon_dir):
        result = design.icon("swatch", "#00ff00")

        image = result.pixmap(24, 24).toImage()
        center = image.pixelColor(12, 12)
        assert (center.red(), center.green(), center.blue()) == (0, 255, 0)

    def test_missing_icon_name_raises(self, solid_icon_dir):
        with pytest.raises(ValueError):
            design.icon("not-a-real-icon", QColor("#ffffff"))

    def test_every_vendored_icon_loads(self):
        # Exercises the real, checked-in assets (not the synthetic fixture
        # above) so a corrupt or renamed SVG in design/icons/ fails a test
        # instead of only surfacing at runtime.
        names = [path.stem for path in design._ICON_DIR.glob("*.svg")]
        assert names, "expected at least one vendored icon"
        for name in names:
            result = design.icon(name, QColor("#f8faf0"))
            assert isinstance(result, QIcon)
            assert not result.pixmap(24, 24).isNull()


class TestColor:
    def test_resolves_hex_and_alpha_together(self):
        result = design.color("BAR_BG")

        assert result.name() == tokens.Color.BAR_BG
        # QColor quantizes alpha to 8 bits internally, so alphaF() round-trips
        # to e.g. 0.930007 rather than 0.93 exactly — abs tolerance covers
        # that rounding without loosening the check enough to miss a real
        # wrong-alpha bug (one 8-bit step is ~0.0039).
        assert result.alphaF() == pytest.approx(tokens.Color.BAR_BG_ALPHA, abs=1e-3)

    def test_a_token_with_no_alpha_sibling_resolves_fully_opaque(self):
        # TEXT_PRIMARY has no TEXT_PRIMARY_ALPHA in tokens.py — plain,
        # opaque colours should still come back as a complete QColor
        # rather than requiring callers to know that.
        assert not hasattr(tokens.Color, "TEXT_PRIMARY_ALPHA")

        result = design.color("TEXT_PRIMARY")

        assert result.name() == tokens.Color.TEXT_PRIMARY
        assert result.alphaF() == pytest.approx(1.0)

    def test_unknown_token_name_raises(self):
        with pytest.raises(ValueError):
            design.color("NOT_A_REAL_TOKEN")


class _FakeFontDatabase:
    """Stand-in for the QFontDatabase class object itself, matching
    test_capture.py's `_FakeQGuiApplication` pattern: font_families() calls
    QFontDatabase class-style (`.families()`, `.systemFont()`,
    `.addApplicationFont()`) without instantiating, so monkeypatching the
    module's `QFontDatabase` name to an instance of this stand-in works the
    same way attribute lookup would on the real class.
    """

    class SystemFont:
        GeneralFont = "GeneralFont"
        FixedFont = "FixedFont"

    def __init__(self, installed):
        self._installed = installed
        self.added_paths = []

    def families(self):
        return self._installed

    def addApplicationFont(self, path):
        self.added_paths.append(path)
        return 0

    def systemFont(self, which):
        family = "Fallback Sans" if which == self.SystemFont.GeneralFont else "Fallback Mono"
        return _FakeQFont(family)


class _FakeQFont:
    def __init__(self, family):
        self._family = family

    def family(self):
        return self._family


class TestFontFamilies:
    def test_falls_back_to_a_system_family_when_plex_is_not_installed(self, monkeypatch):
        monkeypatch.setattr(design, "_FONT_DIR", design._FONT_DIR.parent / "no-such-dir")
        monkeypatch.setattr(design, "QFontDatabase", _FakeFontDatabase(installed=[]))

        result = design.font_families()

        assert result.ui == "Fallback Sans"
        assert result.mono == "Fallback Mono"

    def test_uses_plex_when_it_is_registered(self, monkeypatch):
        monkeypatch.setattr(design, "_FONT_DIR", design._FONT_DIR.parent / "no-such-dir")
        monkeypatch.setattr(
            design,
            "QFontDatabase",
            _FakeFontDatabase(installed=[tokens.Font.UI, tokens.Font.MONO]),
        )

        result = design.font_families()

        assert result.ui == tokens.Font.UI
        assert result.mono == tokens.Font.MONO

    def test_registers_bundled_font_files_before_checking(self, tmp_path, monkeypatch):
        # design/fonts/ doesn't exist in this handoff (the ticket says the
        # font files themselves weren't provided), but the loader still has
        # to try registering whatever's there before falling back — this
        # simulates a future drop of real .ttf files without needing any.
        font_dir = tmp_path / "fonts"
        font_dir.mkdir()
        fake_font_file = font_dir / "IBMPlexSans-Regular.ttf"
        fake_font_file.write_bytes(b"not a real font, just needs to exist")
        monkeypatch.setattr(design, "_FONT_DIR", font_dir)
        fake_db = _FakeFontDatabase(installed=[])
        monkeypatch.setattr(design, "QFontDatabase", fake_db)

        design.font_families()

        assert fake_db.added_paths == [str(fake_font_file)]

    def test_no_fonts_directory_is_not_an_error(self, monkeypatch):
        monkeypatch.setattr(design, "_FONT_DIR", design._FONT_DIR.parent / "no-such-dir")
        monkeypatch.setattr(design, "QFontDatabase", _FakeFontDatabase(installed=[]))

        result = design.font_families()

        assert result.ui
        assert result.mono
