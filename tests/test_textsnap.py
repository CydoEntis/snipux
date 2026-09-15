"""textsnap: a highlighter sweep fitted to the lines of text under it.

Every image here is drawn by the test, so which pixels are which word is
known rather than guessed: each word is drawn again on its own, at the same
place, and its ink found pixel by pixel. A band is right when it holds all of
the ink of the words it should and none of the words either side.

Sizes are image pixels, the only space `textsnap` knows.
"""

import math
import random

import pytest
from PyQt6.QtCore import QPointF, QRectF
from PyQt6.QtGui import QColor, QFont, QFontMetricsF, QImage, QPainter
from PyQt6.QtWidgets import QApplication

from snipux.textsnap import text_bands


@pytest.fixture(scope="module", autouse=True)
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


LINES = ["alpha beta gamma delta", "epsilon zeta eta theta", "iota kappa lambda mu"]
LEFT = 20
FIRST_BASELINE = 40
LEADING = 30
TEXT_PX = 18
WIDTH, HEIGHT = 520, 130
# The highlighter's first-run stroke, 5, at HIGHLIGHT_MULT.
STROKE = 17.5
LIGHT = ("#ffffff", "#202020")
DARK = ("#1e1f24", "#e3c58f")


def _font() -> QFont:
    font = QFont()
    font.setPixelSize(TEXT_PX)
    return font


def _blank(colours=LIGHT, scale=1) -> QImage:
    image = QImage(WIDTH * scale, HEIGHT * scale, QImage.Format.Format_RGB32)
    image.fill(QColor(colours[0]))
    return image


def _page(lines=LINES, colours=LIGHT, scale=1) -> QImage:
    image = _blank(colours, scale)
    painter = QPainter(image)
    painter.scale(scale, scale)
    painter.setFont(_font())
    painter.setPen(QColor(colours[1]))
    for index, text in enumerate(lines):
        painter.drawText(QPointF(LEFT, FIRST_BASELINE + index * LEADING), text)
    painter.end()
    return image


def _word_x(line: int, word: int, lines=LINES) -> tuple[float, float]:
    """Where word `word` of line `line` starts and ends, by advance."""
    words = lines[line].split(" ")
    metrics = QFontMetricsF(_font())
    prefix = " ".join(words[:word]) + (" " if word else "")
    start = LEFT + metrics.horizontalAdvance(prefix)
    return start, start + metrics.horizontalAdvance(words[word])


def _word_ink(line: int, word: int, lines=LINES, colours=LIGHT) -> QRectF:
    """The ink of one word, drawn alone where the page draws it."""
    image = _blank(colours)
    painter = QPainter(image)
    painter.setFont(_font())
    painter.setPen(QColor(colours[1]))
    painter.drawText(QPointF(_word_x(line, word, lines)[0], FIRST_BASELINE + line * LEADING),
                     lines[line].split(" ")[word])
    painter.end()
    return _ink_rect(image, QColor(colours[0]))


def _ink_rect(image: QImage, background: QColor) -> QRectF:
    grey = image.convertToFormat(QImage.Format.Format_Grayscale8)
    base = QColor(background).lightness()
    bits = grey.constBits()
    bits.setsize(grey.sizeInBytes())
    data = bytes(bits)
    stride = grey.bytesPerLine()
    xs, ys = [], []
    for y in range(grey.height()):
        row = data[y * stride : y * stride + grey.width()]
        for x, value in enumerate(row):
            if abs(value - base) >= 56:
                xs.append(x)
                ys.append(y)
    return QRectF(min(xs), min(ys), max(xs) - min(xs) + 1, max(ys) - min(ys) + 1)


def _mid(line: int) -> float:
    """The middle of a line's x-height."""
    return FIRST_BASELINE + line * LEADING - TEXT_PX * 0.28


def _sweep(x0: float, x1: float, y: float, wobble: float = 0.0) -> list[QPointF]:
    step = 5 if x1 >= x0 else -5
    count = max(2, int(abs(x1 - x0) / 5) + 1)
    return [
        QPointF(x0 + i * step, y + wobble * math.sin((x0 + i * step) / 17)) for i in range(count)
    ]


def _covers(band: QRectF, ink: QRectF) -> bool:
    return band.contains(ink)


def _touches(band: QRectF, ink: QRectF) -> bool:
    return band.intersects(ink)


class TestOneLine:
    @pytest.mark.parametrize("colours", [LIGHT, DARK], ids=["light", "dark"])
    def test_a_sweep_inside_two_words_takes_both_of_them_whole(self, colours):
        beta, gamma = _word_x(0, 1), _word_x(0, 2)
        stroke = _sweep((beta[0] + beta[1]) / 2, (gamma[0] + gamma[1]) / 2, _mid(0))

        bands = text_bands(_page(colours=colours), stroke, STROKE)

        assert len(bands) == 1
        assert _covers(bands[0], _word_ink(0, 1, colours=colours))
        assert _covers(bands[0], _word_ink(0, 2, colours=colours))
        assert not _touches(bands[0], _word_ink(0, 0, colours=colours))
        assert not _touches(bands[0], _word_ink(0, 3, colours=colours))

    def test_a_sweep_from_space_to_space_takes_the_words_between(self):
        alpha, beta = _word_x(0, 0), _word_x(0, 1)
        gamma, delta = _word_x(0, 2), _word_x(0, 3)
        stroke = _sweep((alpha[1] + beta[0]) / 2, (gamma[1] + delta[0]) / 2, _mid(0))

        (band,) = text_bands(_page(), stroke, STROKE)

        assert _covers(band, _word_ink(0, 1)) and _covers(band, _word_ink(0, 2))
        assert not _touches(band, _word_ink(0, 0))
        assert not _touches(band, _word_ink(0, 3))

    def test_a_wobbly_sweep_snaps_to_the_band_a_straight_one_does(self):
        start, end = _word_x(0, 0)[0] + 4, _word_x(0, 2)[1] - 4
        page = _page()

        straight = text_bands(page, _sweep(start, end, _mid(0)), STROKE)
        wobbly = text_bands(page, _sweep(start, end, _mid(0), wobble=5), STROKE)

        assert wobbly == straight

    def test_the_band_is_the_height_of_the_type_not_of_its_letters(self):
        # No ascenders or descenders on one line, both on the other: the same
        # type, so the same band, where a band fitted to the ink would be
        # half the height on the first.
        lines = ["crane ocean sources", "height policy bright"]
        page = _page(lines)

        (low,) = text_bands(page, _sweep(LEFT + 4, _word_x(0, 1, lines)[1], _mid(0)), STROKE)
        (tall,) = text_bands(page, _sweep(LEFT + 4, _word_x(1, 1, lines)[1], _mid(1)), STROKE)

        assert abs(low.height() - tall.height()) <= 3
        assert abs((low.bottom() + LEADING) - tall.bottom()) <= 3

    def test_a_sweep_along_the_gap_under_a_line_snaps_to_that_line_only(self):
        # Closer to the line above than the one below: it goes to one line,
        # never a band on both.
        stroke = _sweep(LEFT + 4, _word_x(0, 1)[1], FIRST_BASELINE + 3)

        bands = text_bands(_page(), stroke, STROKE)

        assert len(bands) == 1
        assert _covers(bands[0], _word_ink(0, 0))


class TestTightlyPackedLines:
    """A terminal or a dense editor: a few blank rows between lines, or none.
    Found as one block of three lines on a 125% display, where the stroke's
    width in frame pixels bridged the gaps between them."""

    TIGHT = 21  # line pitch for 18px type: three or so blank rows, or touching

    def _tight_page(self, scale=1) -> QImage:
        image = _blank(scale=scale)
        painter = QPainter(image)
        painter.scale(scale, scale)
        painter.setFont(_font())
        painter.setPen(QColor(LIGHT[1]))
        for index, text in enumerate(LINES):
            painter.drawText(QPointF(LEFT, FIRST_BASELINE + index * self.TIGHT), text)
        painter.end()
        return image

    def _ink(self, line, word, scale=1) -> QRectF:
        image = _blank(scale=scale)
        painter = QPainter(image)
        painter.scale(scale, scale)
        painter.setFont(_font())
        painter.setPen(QColor(LIGHT[1]))
        painter.drawText(
            QPointF(_word_x(line, word)[0], FIRST_BASELINE + line * self.TIGHT),
            LINES[line].split(" ")[word],
        )
        painter.end()
        return _ink_rect(image, QColor(LIGHT[0]))

    def _mid(self, line) -> float:
        return FIRST_BASELINE + line * self.TIGHT - TEXT_PX * 0.28

    @pytest.mark.parametrize("display", [1.0, 1.25, 1.5, 2.0])
    def test_a_swipe_along_the_middle_line_takes_that_line_alone(self, display):
        # The stroke is as wide in frame pixels as a scaled display makes it;
        # the page itself stays at 1x, so only the stroke's reach changes.
        stroke = _sweep(LEFT + 4, _word_x(1, 2)[1] - 4, self._mid(1), wobble=3)

        bands = text_bands(self._tight_page(), stroke, STROKE * display)

        assert len(bands) == 1
        assert _covers(bands[0], self._ink(1, 0)) and _covers(bands[0], self._ink(1, 2))
        for word in range(4):
            assert not _touches(bands[0], self._ink(0, word))
            assert not _touches(bands[0], self._ink(2, word))

    def test_on_a_2x_frame_a_swipe_takes_one_line(self):
        stroke = [
            QPointF(point.x() * 2, point.y() * 2)
            for point in _sweep(LEFT + 4, _word_x(0, 2)[1] - 4, self._mid(0), wobble=3)
        ]

        bands = text_bands(self._tight_page(scale=2), stroke, STROKE * 2)

        assert len(bands) == 1
        for word in range(4):
            assert not _touches(bands[0], self._ink(1, word, scale=2))

    def test_a_swipe_that_drifts_onto_the_next_line_at_its_end_takes_one_line(self):
        # A hand coming off the mouse: the last stretch sags onto the line
        # below. That is not a drag selection of two lines.
        body = _sweep(LEFT + 4, _word_x(1, 2)[1], self._mid(1))
        sag = [QPointF(body[-1].x() + 5 * i, self._mid(1) + 3 * i) for i in range(1, 6)]

        bands = text_bands(self._tight_page(), body + sag, STROKE)

        assert len(bands) == 1
        assert _covers(bands[0], self._ink(1, 0))
        assert not _touches(bands[0], self._ink(2, 0))


class TestSeveralLines:
    def test_a_swipe_per_line_keeps_each_lines_own_words(self):
        stroke = (
            _sweep(LEFT + 4, _word_x(0, 1)[1] - 4, _mid(0))
            + _sweep(_word_x(1, 1)[1] - 4, LEFT + 4, _mid(1))
            + _sweep(LEFT + 4, _word_x(2, 1)[1] - 4, _mid(2))
        )

        bands = text_bands(_page(), stroke, STROKE)

        assert len(bands) == 3
        top, middle, bottom = bands
        assert _covers(top, _word_ink(0, 0)) and _covers(top, _word_ink(0, 1))
        assert not _touches(top, _word_ink(0, 2))
        assert _covers(middle, _word_ink(1, 0)) and _covers(middle, _word_ink(1, 1))
        assert not _touches(middle, _word_ink(1, 2))
        assert _covers(bottom, _word_ink(2, 0)) and _covers(bottom, _word_ink(2, 1))
        assert not _touches(bottom, _word_ink(2, 2))

    def test_bands_on_neighbouring_lines_do_not_overlap(self):
        stroke = _sweep(LEFT + 4, 300, _mid(0)) + _sweep(300, LEFT + 4, _mid(1))

        top, bottom = text_bands(_page(), stroke, STROKE)

        assert top.bottom() <= bottom.top()

    def test_a_diagonal_drag_selects_the_way_a_text_editor_does(self):
        beta, kappa = _word_x(0, 1), _word_x(2, 1)
        start = QPointF((beta[0] + beta[1]) / 2, _mid(0))
        end = QPointF((kappa[0] + kappa[1]) / 2, _mid(2))
        stroke = [start + (end - start) * (i / 40) for i in range(41)]

        bands = text_bands(_page(), stroke, STROKE)

        assert len(bands) == 3
        top, middle, bottom = bands
        # From the word it began on to the end of its line...
        assert _covers(top, _word_ink(0, 1)) and _covers(top, _word_ink(0, 3))
        assert not _touches(top, _word_ink(0, 0))
        # ...every line between, whole...
        assert _covers(middle, _word_ink(1, 0)) and _covers(middle, _word_ink(1, 3))
        # ...and from the start of the last to the word it ended on.
        assert _covers(bottom, _word_ink(2, 0)) and _covers(bottom, _word_ink(2, 1))
        assert not _touches(bottom, _word_ink(2, 2))

    def test_the_same_drag_made_bottom_up_selects_the_same(self):
        beta, kappa = _word_x(0, 1), _word_x(2, 1)
        start = QPointF((beta[0] + beta[1]) / 2, _mid(0))
        end = QPointF((kappa[0] + kappa[1]) / 2, _mid(2))
        down = [start + (end - start) * (i / 40) for i in range(41)]
        page = _page()

        assert text_bands(page, down[::-1], STROKE) == text_bands(page, down, STROKE)


class TestNothingToSnapTo:
    def test_a_sweep_over_no_text_gives_no_bands(self):
        assert text_bands(_blank(), _sweep(20, 400, 50), STROKE) == []

    def test_a_sweep_over_a_photo_gives_no_bands(self):
        # No one grey is the background, so nothing is ink on it.
        image = _blank()
        noise = random.Random(7)
        for y in range(image.height()):
            for x in range(image.width()):
                grey = noise.randrange(256)
                image.setPixelColor(x, y, QColor(grey, grey, grey))

        assert text_bands(image, _sweep(20, 400, 50), STROKE) == []

    def test_a_single_point_is_no_sweep(self):
        assert text_bands(_page(), [QPointF(60, _mid(0))], STROKE) == []

    def test_a_sweep_outside_the_image_gives_no_bands(self):
        assert text_bands(_page(), _sweep(20, 400, HEIGHT + 200), STROKE) == []


class TestScale:
    def test_a_page_at_twice_the_pixels_snaps_to_twice_the_band(self):
        # The same page on a 2x display: every length -- the stroke, its
        # width, the band -- is in that image's pixels.
        stroke = _sweep(_word_x(0, 1)[0] + 3, _word_x(0, 2)[1] - 3, _mid(0))
        (once,) = text_bands(_page(), stroke, STROKE)

        doubled = [QPointF(point.x() * 2, point.y() * 2) for point in stroke]
        (twice,) = text_bands(_page(scale=2), doubled, STROKE * 2)

        for single, double in (
            (once.left(), twice.left()),
            (once.right(), twice.right()),
            (once.top(), twice.top()),
            (once.bottom(), twice.bottom()),
        ):
            assert abs(single * 2 - double) <= 4
