"""Design-system loader: icons, fonts and token-backed colour resolution.

`design/tokens.py` and `design/icons/*.svg` are vendored data; nothing in
the app actually loads them until this module. Three entry points, per
docs/design/overlay-redesign.md:

- `icon(name, color)` — recolours one of the 24x24 `currentColor` SVGs and
  returns a QIcon.
- `color(token_name)` — resolves a `tokens.Color` attribute to a QColor with
  its alpha applied, so callers never juggle a hex string and a separate
  alpha constant themselves.
- `font_families()` — registers the bundled IBM Plex fonts if present,
  degrading to a system sans/mono family otherwise. The font files are not
  part of this handoff and must never be fetched at build time (see the
  ticket), so a missing `design/fonts/` directory is a normal, silent case,
  not an error.

Widget code should import from here rather than re-typing any hex, metric or
font name from the design — `tokens.py` is the single source of truth, and
this module is the only thing that resolves it into Qt objects.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PyQt6.QtCore import QByteArray, Qt
from PyQt6.QtGui import QColor, QFontDatabase, QIcon, QPainter, QPixmap
from PyQt6.QtSvg import QSvgRenderer

from . import tokens

_DESIGN_DIR = Path(__file__).resolve().parent
_ICON_DIR = _DESIGN_DIR / "icons"
_FONT_DIR = _DESIGN_DIR / "fonts"


def icon(name: str, color: QColor | str) -> QIcon:
    """Return a QIcon for design icon `name`, recoloured to `color`.

    The vendored SVGs use `stroke="currentColor"` (overlay-redesign.md:
    "recolour by parsing and substituting currentColor at load time")
    because a single glyph is reused at several different colours (idle,
    hover, active, danger...) depending on button state. Substitution
    happens on the raw SVG text before rendering — `currentColor` has no
    meaning to `QSvgRenderer` on its own, so rasterising first and
    recolouring the pixmap after would need a colour-replace pass anyway,
    and would lose antialiasing at the glyph's edges in the process.
    """
    path = _ICON_DIR / f"{name}.svg"
    if not path.is_file():
        # A missing icon is a programming error (a typo'd name, or a glyph
        # never vendored), not a runtime condition a caller should have to
        # handle — raising here is what keeps a typo from silently painting
        # a blank button instead of failing the build/test that exercises it.
        raise ValueError(f"no such design icon: {name!r}")

    qcolor = color if isinstance(color, QColor) else QColor(color)
    svg_text = path.read_text(encoding="utf-8").replace("currentColor", qcolor.name())

    renderer = QSvgRenderer(QByteArray(svg_text.encode("utf-8")))
    pixmap = QPixmap(renderer.defaultSize())
    pixmap.fill(Qt.GlobalColor.transparent)

    painter = QPainter(pixmap)
    renderer.render(painter)
    # Closed before the QIcon reads the pixmap below — never leave a
    # QPainter open across a read of the pixmap it is painting, per
    # CLAUDE.md.
    painter.end()

    return QIcon(pixmap)


def win_color(token_name: str) -> QColor:
    """`tokens.Win.<token_name>` -> QColor, alpha included.

    The same pairing rule `color()` applies to the overlay's palette, for
    the Settings/review chrome: `OK_BG` + `OK_BG_ALPHA` is one colour, and
    resolving it in one place is what stops a caller re-typing the
    percentage into an rgba() string that then drifts from the token.
    """
    if not hasattr(tokens.Win, token_name):
        raise ValueError(f"no such window colour: {token_name!r}")
    qcolor = QColor(getattr(tokens.Win, token_name))
    qcolor.setAlphaF(getattr(tokens.Win, f"{token_name}_ALPHA", 1.0))
    return qcolor


def chooser_color(token_name: str) -> QColor:
    """`tokens.ChooserColor.<token_name>` -> QColor, alpha included.

    The same colour+alpha pairing `color()` and `win_color()` apply, for the
    pre-snip chooser's palette. Resolving it in one place is what stops a
    caller re-typing a percentage that then drifts from tokens.py.
    """
    if not hasattr(tokens.ChooserColor, token_name):
        raise ValueError(f"no such chooser colour: {token_name!r}")
    qcolour = QColor(getattr(tokens.ChooserColor, token_name))
    qcolour.setAlphaF(getattr(tokens.ChooserColor, f"{token_name}_ALPHA", 1.0))
    return qcolour


def color(token_name: str) -> QColor:
    """Resolve `tokens.Color.<token_name>` to a QColor, alpha included.

    Several `Color` entries are a hex/alpha pair — `BAR_BG` +
    `BAR_BG_ALPHA` — because the design paints them as a translucent fill.
    Looking up the sibling `<token_name>_ALPHA` constant here, rather than
    leaving it to callers, is what the acceptance criterion means by "a
    colour and its alpha are never applied separately": there is exactly
    one call that produces a fully-specified QColor. Tokens with no such
    sibling (most of them — plain, opaque colours) resolve at alpha 1.0.
    """
    if not hasattr(tokens.Color, token_name):
        raise ValueError(f"no such design colour: {token_name!r}")

    qcolor = QColor(getattr(tokens.Color, token_name))
    alpha = getattr(tokens.Color, f"{token_name}_ALPHA", 1.0)
    qcolor.setAlphaF(alpha)
    return qcolor


@dataclass(frozen=True)
class FontFamilies:
    """The resolved UI (sans) and mono family names to build QFonts from."""

    ui: str
    mono: str


def _load_bundled_fonts() -> None:
    """Register any font files under `design/fonts/` with Qt's font database.

    The IBM Plex weights specified in overlay-redesign.md are not in this
    handoff and must not be downloaded during a build (per the ticket), so
    this is a deliberate no-op — not an error — whenever that directory is
    absent or empty. It exists purely so a future ticket can drop the actual
    files in without any loader change: `font_families()` below already
    checks the font database rather than the filesystem, so registering them
    here is the only wiring needed.
    """
    if not _FONT_DIR.is_dir():
        return
    for font_path in list(_FONT_DIR.glob("*.ttf")) + list(_FONT_DIR.glob("*.otf")):
        QFontDatabase.addApplicationFont(str(font_path))


def font_families() -> FontFamilies:
    """Resolve the UI and mono font families named in `tokens.Font`.

    Falls back to the platform's own general/fixed-pitch family when IBM
    Plex isn't registered (either not bundled, or bundled but the file
    turned out unreadable) — `QFontDatabase.families()` is checked, not the
    filesystem, so a corrupt font file degrades the same way a missing one
    does rather than raising.
    """
    _load_bundled_fonts()
    installed = set(QFontDatabase.families())

    ui = tokens.Font.UI
    if ui not in installed:
        ui = QFontDatabase.systemFont(QFontDatabase.SystemFont.GeneralFont).family()

    mono = tokens.Font.MONO
    if mono not in installed:
        mono = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont).family()

    return FontFamilies(ui=ui, mono=mono)
