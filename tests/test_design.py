import inspect
import re
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
from PyQt6.QtCore import QRect
from PyQt6.QtGui import QColor, QIcon, QImage, QPainter
from PyQt6.QtWidgets import QApplication

import snipux.design as design
from snipux.design import tokens
from snipux.design import PACKAGE_DIR

LOGO_DIR = PACKAGE_DIR / "design" / "logo"

_REPO_ROOT = Path(__file__).resolve().parent.parent

# Matches a Color token assignment whose trailing comment names a percentage,
# e.g. `ICON_ACTIVE_BG  = "#ffffff"   # at 16% alpha`. Capturing the
# percentage lets the test check the sibling _ALPHA constant carries that
# exact value, not merely that one exists.
_ALPHA_COMMENT_RE = re.compile(
    r"^\s*([A-Z][A-Z0-9_]*)\s*=\s*[\"'].*[\"'].*#.*?(\d+(?:\.\d+)?)%\s*alpha", re.MULTILINE
)

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


class TestFlowColor:
    """The capture flow's palette (docs/design/flow). Same pairing rule as
    `TestColor` above -- a fourth palette resolved by a fourth helper, which
    is exactly the shape that lets one of them quietly skip the rule.
    """

    def test_resolves_hex_and_alpha_together(self):
        result = design.flow_color("BAR_BG")

        assert result.name() == tokens.FlowColor.BAR_BG
        assert result.alphaF() == pytest.approx(tokens.FlowColor.BAR_BG_ALPHA, abs=1e-3)

    def test_a_token_with_no_alpha_sibling_resolves_fully_opaque(self):
        assert not hasattr(tokens.FlowColor, "ACCENT_ALPHA")

        result = design.flow_color("ACCENT")

        assert result.name() == tokens.FlowColor.ACCENT
        assert result.alphaF() == pytest.approx(1.0)

    def test_one_colour_carrying_two_alphas_is_two_tokens(self):
        # The window-mode hover preview is one colour at two strengths --
        # an 85% border around a 7% fill. A single token cannot be right
        # for both, and the handoff's own "14-18%" range is the same shape
        # of problem, so each strength gets its own name.
        border = design.flow_color("WINDOW_HOVER")
        fill = design.flow_color("WINDOW_HOVER_FILL")

        assert border.name() == fill.name()
        assert border.alphaF() > fill.alphaF()

    def test_unknown_token_name_raises(self):
        with pytest.raises(ValueError):
            design.flow_color("NOT_A_REAL_TOKEN")


class TestTokenAlphaComments:
    """SNX-60: a token whose comment names an alpha percentage but has no
    matching `_ALPHA` constant is silently opaque -- `design.color()` has no
    way to know the comment's prose was ever meant to apply. ICON_ACTIVE_BG
    and ICON_HOVER_BG were exactly this bug. This scans the source of
    tokens.py itself so a future colour added the same broken way fails
    here, rather than only being caught by whoever happens to eyeball the
    rendered button.
    """

    # Every palette, each resolved by its own helper: Color for the overlay,
    # Win for the Settings/review chrome, FlowColor for the capture-flow
    # bars, BarColor for the locked bars handoff.
    PALETTES = (tokens.Color, tokens.Win, tokens.FlowColor, tokens.BarColor)

    def _alpha_comments(self):
        """(palette, name, percent) for each colour whose comment names an
        alpha, read against the class that defines it. Two palettes can share
        a name at different alphas -- BAR_BG is 93% in Color and 94% in
        BarColor -- so a lookup by name alone checks the wrong one.
        """
        return [
            (palette, name, percent)
            for palette in self.PALETTES
            for name, percent in _ALPHA_COMMENT_RE.findall(inspect.getsource(palette))
        ]

    def test_every_alpha_comment_is_in_a_palette_checked_here(self):
        # A palette missing from PALETTES is one that can reintroduce the
        # silently-opaque bug this class exists for.
        in_module = _ALPHA_COMMENT_RE.findall(inspect.getsource(tokens))
        assert in_module, "expected to find at least one alpha-in-comment colour token"

        assert len(in_module) == len(self._alpha_comments())

    def test_every_alpha_named_in_a_comment_has_a_matching_alpha_constant(self):
        missing = [
            f"{palette.__name__}.{name}"
            for palette, name, _percent in self._alpha_comments()
            if not hasattr(palette, f"{name}_ALPHA")
        ]
        assert not missing, (
            f"{missing} name an alpha in a comment but have no matching "
            f"<name>_ALPHA constant for design.color() to apply"
        )

    def test_every_alpha_constant_matches_the_percentage_its_comment_names(self):
        for palette, name, percent in self._alpha_comments():
            actual = getattr(palette, f"{name}_ALPHA")
            assert actual == pytest.approx(float(percent) / 100), (
                f"{palette.__name__}.{name}_ALPHA is {actual}, but its comment "
                f"names {percent}%"
            )


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

    def test_prefers_a_real_face_over_qts_own_category(self, monkeypatch):
        # Qt's "fixed" family is a category, not a face: on Windows it is
        # Courier New, which is what made every path in Settings hard to
        # read. Anything on the list beats it.
        monkeypatch.setattr(design, "_FONT_DIR", design._FONT_DIR.parent / "no-such-dir")
        monkeypatch.setattr(
            design,
            "QFontDatabase",
            _FakeFontDatabase(installed=["Segoe UI", "Cascadia Mono", "Courier New"]),
        )

        result = design.font_families()

        assert result.ui == "Segoe UI"
        assert result.mono == "Cascadia Mono"

    def test_the_list_is_tried_in_order(self, monkeypatch):
        monkeypatch.setattr(design, "_FONT_DIR", design._FONT_DIR.parent / "no-such-dir")
        monkeypatch.setattr(
            design,
            "QFontDatabase",
            _FakeFontDatabase(installed=["Consolas", "Cascadia Mono", "DejaVu Sans"]),
        )

        result = design.font_families()

        assert result.mono == "Cascadia Mono"      # ahead of Consolas on the list
        assert result.ui == "DejaVu Sans"

    def test_plex_still_wins_when_it_is_there(self, monkeypatch):
        monkeypatch.setattr(design, "_FONT_DIR", design._FONT_DIR.parent / "no-such-dir")
        monkeypatch.setattr(
            design,
            "QFontDatabase",
            _FakeFontDatabase(installed=[tokens.Font.UI, tokens.Font.MONO, "Segoe UI", "Consolas"]),
        )

        result = design.font_families()

        assert result.ui == tokens.Font.UI
        assert result.mono == tokens.Font.MONO

    def test_qts_choice_is_the_last_resort_not_the_first(self, monkeypatch):
        monkeypatch.setattr(design, "_FONT_DIR", design._FONT_DIR.parent / "no-such-dir")
        monkeypatch.setattr(design, "QFontDatabase", _FakeFontDatabase(installed=["Comic Sans MS"]))

        result = design.font_families()

        assert result.ui == "Fallback Sans"
        assert result.mono == "Fallback Mono"

    def test_every_fallback_is_named_once(self):
        for names in (tokens.Font.UI_FALLBACKS, tokens.Font.MONO_FALLBACKS):
            assert len(set(names)) == len(names)

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


@pytest.fixture(scope="module")
def wheel_contents(tmp_path_factory):
    # Builds the real wheel and inspects it, rather than importing from the
    # source tree -- that's exactly the gap SNX-56 fell through: an explicit
    # `packages = ["snipux"]` list silently dropped the design subpackage,
    # and design/icons/*.svg were never declared as data, so the install had
    # no icons in it even once the subpackage was found. Only a check
    # against the built artifact catches that class of bug.
    out_dir = tmp_path_factory.mktemp("snipux-wheel")
    # --no-deps: this only needs to prove what's *packaged*, not resolve
    # PyQt6/jeepney again. --no-build-isolation: build with the
    # setuptools/wheel already installed from requirements.txt instead of
    # pip fetching a second, isolated copy of them.
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            str(_REPO_ROOT),
            "--no-deps",
            "--no-build-isolation",
            "--wheel-dir",
            str(out_dir),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    wheel_paths = list(out_dir.glob("snipux-*.whl"))
    assert len(wheel_paths) == 1, f"expected exactly one built wheel, got {wheel_paths}"

    with zipfile.ZipFile(wheel_paths[0]) as archive:
        return set(archive.namelist())


class TestPackagedDistribution:
    def test_design_subpackage_is_importable_from_the_wheel(self, wheel_contents):
        assert "snipux/design/__init__.py" in wheel_contents
        assert "snipux/design/tokens.py" in wheel_contents

    def test_every_vendored_icon_is_in_the_wheel(self, wheel_contents):
        icon_dir = _REPO_ROOT / "snipux" / "design" / "icons"
        expected = {f"snipux/design/icons/{path.name}" for path in icon_dir.glob("*.svg")}
        assert expected, "expected at least one vendored icon in the source tree"
        assert expected <= wheel_contents

    def test_every_vendored_logo_file_is_in_the_wheel(self, wheel_contents):
        # SNX-81: design/logo/*.png (the tray/app/desktop-entry artwork) is
        # read at runtime by app.py's load_app_icon() and setup_desktop.py's
        # install_icons() the same way icons/*.svg is read by design/
        # __init__.py -- the same undeclared-package-data gap SNX-56 fixed
        # for icons/*.svg would silently drop this too.
        logo_dir = _REPO_ROOT / "snipux" / "design" / "logo"
        expected = {f"snipux/design/logo/{path.name}" for path in logo_dir.glob("*.png")}
        assert expected, "expected at least one vendored logo file in the source tree"
        assert expected <= wheel_contents

    def test_font_files_are_in_the_wheel_when_present(self, wheel_contents):
        # design/fonts/ is empty in this handoff (see design/__init__.py),
        # so this is written to hold once IBM Plex is vendored rather than
        # to assert anything about today's checkout.
        font_dir = _REPO_ROOT / "snipux" / "design" / "fonts"
        expected = (
            {f"snipux/design/fonts/{path.name}" for path in font_dir.glob("*") if path.is_file()}
            if font_dir.is_dir()
            else set()
        )
        assert expected <= wheel_contents


class TestIconsAreOpticallyCentred:
    """Every glyph has to sit in the middle of its own box.

    They are drawn on a shared 24x24 viewBox, but nothing enforces that the
    ink inside it is centred -- and one that is not reads as misaligned in a
    row of buttons however carefully the buttons themselves are spaced. The
    highlighter's ink was centred on y=15.05 of a box whose middle is 12,
    which put it visibly low beside the pen.

    Sizes are deliberately not asserted: a wide rectangle and a tall droplet
    legitimately differ in aspect. Position does not.
    """

    # Generous enough for a glyph with a deliberate asymmetry (the eraser's
    # tail sits below its body), tight enough that the highlighter's old
    # 3.6-unit drop would not have passed.
    CENTRE_TOLERANCE = 1.6

    def _ink_centre(self, name: str) -> tuple[float, float]:
        """The centre of the glyph's ink, as an offset from the box's own
        centre, in percent of the box.
        """
        size = 96
        image = QImage(size, size, QImage.Format.Format_ARGB32)
        image.fill(0)
        painter = QPainter(image)
        design.icon(name, "#ffffff").paint(painter, QRect(0, 0, size, size))
        painter.end()

        xs, ys = [], []
        for y in range(size):
            for x in range(size):
                if image.pixelColor(x, y).alpha() > 24:
                    xs.append(x)
                    ys.append(y)
        assert xs, f"{name} rendered nothing"
        cx = (min(xs) + max(xs) + 1) / 2 / size * 100 - 50
        cy = (min(ys) + max(ys) + 1) / 2 / size * 100 - 50
        return cx, cy

    # Glyphs, not tool ids: Pixelate is drawn with `mask` and has no icon of
    # its own.
    @pytest.mark.parametrize(
        "name", sorted({tokens.TOOL_GLYPHS.get(tool, tool) for tool in tokens.TOOLS})
    )
    def test_every_tool_glyph_is_centred(self, name):
        cx, cy = self._ink_centre(name)

        assert abs(cx) <= self.CENTRE_TOLERANCE, f"{name} is {cx:+.1f}% off horizontally"
        assert abs(cy) <= self.CENTRE_TOLERANCE, f"{name} is {cy:+.1f}% off vertically"

    @pytest.mark.parametrize("name", ["undo", "redo", "trash", "copy", "save"])
    def test_every_action_glyph_is_centred(self, name):
        cx, cy = self._ink_centre(name)

        assert abs(cx) <= self.CENTRE_TOLERANCE, f"{name} is {cx:+.1f}% off horizontally"
        assert abs(cy) <= self.CENTRE_TOLERANCE, f"{name} is {cy:+.1f}% off vertically"

    def test_the_highlighter_matches_the_pen_it_sits_beside(self):
        # The specific complaint: side by side, one looked lower than the
        # other.
        _pen_x, pen_y = self._ink_centre("pen")
        _hl_x, hl_y = self._ink_centre("highlighter")

        assert abs(pen_y - hl_y) <= 1.0


class TestThereIsOneGreen:
    """There were five, within a few percent of each other: the accent, a
    success text colour, a success fill, a "Saved" tick, a filename preview
    -- and the app icon's own. Read together they looked like an imprecise
    palette rather than five meanings. Success and the primary action are
    the same green now; weight and alpha are what separate them.
    """

    ACCENTS = ("ACCENT", "ACCENT_SOFT")

    def test_every_status_green_is_the_accent(self):
        greens = {
            "OK_FG": tokens.Win.OK_FG,
            "OK_BG": tokens.Win.OK_BG,
            "OK_BORDER": tokens.Win.OK_BORDER,
            "OK_STRONG": tokens.Win.OK_STRONG,
            "PATH_FG": tokens.Win.PATH_FG,
        }
        allowed = {getattr(tokens.Color, name) for name in self.ACCENTS}

        assert set(greens.values()) <= allowed, greens

    def test_the_overlay_families_share_the_one_soft_accent(self):
        assert tokens.FlowColor.ACCENT_SOFT == tokens.Color.ACCENT_SOFT
        assert tokens.BarColor.ACCENT_SOFT == tokens.Color.ACCENT_SOFT

    def test_no_stray_green_hangs_around_in_the_tokens(self):
        """Any *other* saturated green in the palette is the thing this
        collapse was about, so a new one has to be a deliberate addition
        here rather than something that quietly appears."""
        strays = {}
        for family in (tokens.Color, tokens.Win, tokens.FlowColor, tokens.BarColor):
            for name, value in vars(family).items():
                if not isinstance(value, str) or not value.startswith("#") or len(value) != 7:
                    continue
                colour = QColor(value)
                hue, saturation, brightness, _ = colour.getHsv()
                if 60 <= hue <= 160 and saturation > 90 and brightness > 120:
                    if value not in {getattr(tokens.Color, n) for n in self.ACCENTS}:
                        strays[f"{family.__name__}.{name}"] = value

        assert not strays, f"greens that are not the accent: {strays}"

    def test_the_app_icon_is_painted_in_the_accent(self):
        """The icon sits beside the window it opens, in the task bar and the
        title bar at once, so a fifth green is seen there more often than
        anywhere else in the app."""
        accent = QColor(tokens.Color.ACCENT)
        # HSL, not HSV: the artwork was recoloured by taking the accent's
        # hue and saturation and keeping each pixel's own lightness, which
        # is what preserves antialiased edges. An HSV saturation reading of
        # those pixels moves with their lightness and so says nothing about
        # whether the colour is the accent.
        accent_hue, accent_sat, _, _ = accent.getHsl()

        for size in (16, 48, 256):
            image = QImage(str(LOGO_DIR / f"snipux-{size}.png"))
            hues = []
            for y in range(image.height()):
                for x in range(image.width()):
                    colour = QColor(image.pixel(x, y))
                    hue, saturation, brightness, _ = colour.getHsv()
                    if 40 <= hue <= 170 and saturation > 90 and brightness > 100:
                        hsl_hue, hsl_sat, _hsl_l, _ = colour.getHsl()
                        hues.append((hsl_hue, hsl_sat))
            assert hues, f"snipux-{size}.png has no marquee green at all"
            # Averaged: antialiasing spreads each edge pixel a little either
            # way, so no single pixel has to be the token exactly.
            mean_hue = sum(h for h, _ in hues) / len(hues)
            mean_sat = sum(s for _, s in hues) / len(hues)
            assert abs(mean_hue - accent_hue) < 6, f"snipux-{size}.png hue {mean_hue}"
            assert abs(mean_sat - accent_sat) < 25, f"snipux-{size}.png sat {mean_sat}"
