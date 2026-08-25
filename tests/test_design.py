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


class TestTokenAlphaComments:
    """SNX-60: a token whose comment names an alpha percentage but has no
    matching `_ALPHA` constant is silently opaque -- `design.color()` has no
    way to know the comment's prose was ever meant to apply. ICON_ACTIVE_BG
    and ICON_HOVER_BG were exactly this bug. This scans the source of
    tokens.py itself so a future colour added the same broken way fails
    here, rather than only being caught by whoever happens to eyeball the
    rendered button.
    """

    def test_every_alpha_named_in_a_comment_has_a_matching_alpha_constant(self):
        source = inspect.getsource(tokens)
        matches = _ALPHA_COMMENT_RE.findall(source)
        assert matches, "expected to find at least one alpha-in-comment colour token"

        # The rule applies to both palettes: Color for the overlay, Win for
        # the Settings/review chrome, each resolved by its own helper.
        missing = [
            name
            for name, _ in matches
            if not hasattr(tokens.Color, f"{name}_ALPHA")
            and not hasattr(tokens.Win, f"{name}_ALPHA")
            and not hasattr(tokens.ChooserColor, f"{name}_ALPHA")
        ]
        assert not missing, (
            f"{missing} name an alpha in a comment but have no matching "
            f"<name>_ALPHA constant for design.color() to apply"
        )

    def test_every_alpha_constant_matches_the_percentage_its_comment_names(self):
        source = inspect.getsource(tokens)
        matches = _ALPHA_COMMENT_RE.findall(source)

        for name, percent in matches:
            expected = float(percent) / 100
            actual = (
                getattr(tokens.Color, f"{name}_ALPHA", None)
                or getattr(tokens.Win, f"{name}_ALPHA", None)
                or getattr(tokens.ChooserColor, f"{name}_ALPHA")
            )
            assert actual == pytest.approx(expected), (
                f"{name}_ALPHA is {actual}, but its comment names {percent}%"
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

    @pytest.mark.parametrize("name", sorted(tokens.TOOLS))
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
