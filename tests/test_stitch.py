"""The full-page stitcher (SNX-2).

Every frame here is synthetic and its overlap is *known*, so a wrong join is
an assertion failure rather than something to squint at. Real pages are the
other half of judging this and are not a test's job -- a test that needed a
browser would pass or fail by what happened to be open.
"""

import pytest
from PyQt6.QtGui import QColor, QImage, QPainter
from PyQt6.QtWidgets import QApplication

from snipux.stitch import (
    StitchError,
    find_overlap,
    row_signatures,
    sticky_bands,
    stitch,
)


@pytest.fixture(scope="module", autouse=True)
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


WIDTH = 40


def page(height: int, seed: int = 0) -> QImage:
    """A tall image where every row is a different colour from every other.

    Row `y` encodes `y` across two channels, so rows stay distinct up to
    65,536 of them. An earlier version used `(y * 7) % 256`, which repeats
    every 256 rows -- and a fixture whose rows genuinely recur far apart
    makes the stitcher look wrong when it is behaving correctly. Real pages
    are like this version: text and images make almost every scanline
    distinct.

    It also means an off-by-one join shows up as a wrong colour rather than
    passing unnoticed.
    """
    image = QImage(WIDTH, height, QImage.Format.Format_RGB32)
    painter = QPainter(image)
    for y in range(height):
        value = y + seed
        painter.fillRect(0, y, WIDTH, 1, QColor(value & 0xFF, (value >> 8) & 0xFF, 0x40))
    painter.end()
    return image


def viewport(source: QImage, top: int, height: int) -> QImage:
    """One screenful of `source`, as a scroll capture would see it."""
    return source.copy(0, top, source.width(), height)


def band(image: QImage, top: int, height: int, colour: QColor) -> QImage:
    """`image` with a flat band painted over it -- a sticky header/footer."""
    out = image.copy()
    painter = QPainter(out)
    painter.fillRect(0, top, out.width(), height, colour)
    painter.end()
    return out


class TestRowSignatures:
    def test_identical_rows_hash_the_same(self):
        source = page(50)

        assert row_signatures(source) == row_signatures(source.copy())

    def test_a_one_pixel_difference_changes_its_row(self):
        # The reason whole scanlines are hashed rather than a sample of
        # them: a join placed on a row that only *looks* the same is how
        # content gets duplicated.
        source = page(20)
        changed = source.copy()
        changed.setPixelColor(17, 9, QColor("#ff00ff"))

        before, after = row_signatures(source), row_signatures(changed)

        assert before[9] != after[9]
        assert [s for i, s in enumerate(before) if i != 9] == [
            s for i, s in enumerate(after) if i != 9
        ]

    def test_a_null_image_has_no_signatures(self):
        assert row_signatures(QImage()) == []


class TestFindOverlap:
    def test_it_measures_a_known_scroll(self):
        source = page(400)
        first = row_signatures(viewport(source, 0, 200))
        second = row_signatures(viewport(source, 150, 200))

        # Scrolled by 150, so 50 rows are in both.
        assert find_overlap(first, second) == 50

    def test_frames_that_abut_exactly_cannot_be_measured(self):
        # They share no rows, so there is nothing to match on and nothing
        # to verify against. A scroll driver must always move *less* than a
        # viewport -- overlap is the only evidence the frames belong
        # together, and without it a join would be an assumption.
        source = page(400)
        first = row_signatures(viewport(source, 0, 200))
        second = row_signatures(viewport(source, 200, 200))

        assert find_overlap(first, second) is None

    def test_a_page_that_did_not_move_is_not_an_overlap(self):
        # How the bottom of a page is *detected* rather than guessed: the
        # frames stopped changing.
        source = page(400)
        same = row_signatures(viewport(source, 0, 200))

        assert find_overlap(same, same) is None

    def test_a_jump_of_more_than_one_screen_is_refused(self):
        source = page(900)
        first = row_signatures(viewport(source, 0, 200))
        far = row_signatures(viewport(source, 600, 200))

        assert find_overlap(first, far) is None

    def test_the_shortest_scroll_wins_on_a_repeating_page(self):
        # A list of near-identical rows matches at several offsets. The
        # smallest scroll is the one that actually happened; a larger one
        # requires the page to have moved further than it was asked to.
        repeating = QImage(WIDTH, 300, QImage.Format.Format_RGB32)
        painter = QPainter(repeating)
        for y in range(300):
            painter.fillRect(0, y, WIDTH, 1, QColor(0, 0, (y % 50) * 5))
        painter.end()

        first = row_signatures(viewport(repeating, 0, 150))
        second = row_signatures(viewport(repeating, 50, 150))

        assert find_overlap(first, second) == 100

    def test_empty_input_is_not_an_overlap(self):
        assert find_overlap([], [1, 2, 3]) is None
        assert find_overlap([1, 2, 3], []) is None


class TestStickyBands:
    def test_an_unchanging_top_run_is_a_header(self):
        source = page(600)
        frames = [
            row_signatures(band(viewport(source, top, 200), 0, 30, QColor("#123456")))
            for top in (0, 100, 200)
        ]

        header, footer = sticky_bands(frames)

        assert header == 30
        assert footer == 0

    def test_an_unchanging_bottom_run_is_a_footer(self):
        source = page(600)
        frames = [
            row_signatures(band(viewport(source, top, 200), 180, 20, QColor("#654321")))
            for top in (0, 100, 200)
        ]

        header, footer = sticky_bands(frames)

        assert header == 0
        assert footer == 20

    def test_an_unchanging_band_in_the_middle_is_left_alone(self):
        # The load-bearing constraint. "Rows identical in every frame" also
        # describes a flat band of page background, and cropping that would
        # delete real content. Only edge-anchored runs count.
        source = page(600)
        frames = [
            row_signatures(band(viewport(source, top, 200), 90, 20, QColor("#0f0f0f")))
            for top in (0, 100, 200)
        ]

        assert sticky_bands(frames) == (0, 0)

    def test_one_frame_has_nothing_to_be_constant_against(self):
        # Everything would look sticky, and the whole frame would be
        # cropped away.
        assert sticky_bands([row_signatures(page(200))]) == (0, 0)


class TestStitch:
    def _column(self, image: QImage) -> list[int]:
        """One pixel per row, as a fingerprint of what survived."""
        return [image.pixel(WIDTH // 2, y) for y in range(image.height())]

    def test_a_scrolled_page_comes_back_whole(self):
        source = page(500)
        frames = [viewport(source, top, 200) for top in (0, 150, 300)]

        result = stitch(frames)

        assert result.height() == 500
        assert self._column(result) == self._column(source)

    def test_one_frame_is_returned_as_it_is(self):
        # A page that fits in one viewport must produce exactly the
        # single-frame result, not a re-derived approximation of it.
        only = page(200)

        result = stitch([only])

        assert result.size() == only.size()
        assert self._column(result) == self._column(only)

    def test_a_sticky_header_survives_exactly_once(self):
        source = page(500)
        frames = [
            band(viewport(source, top, 200), 0, 25, QColor("#112233"))
            for top in (0, 150, 300)
        ]

        result = stitch(frames)
        sticky = QColor("#112233").rgb()
        rows = [y for y, pixel in enumerate(self._column(result)) if pixel == sticky]

        assert rows == list(range(25)), "the header repeated or was lost"

    def test_a_sticky_footer_survives_exactly_once_at_the_bottom(self):
        source = page(500)
        frames = [
            band(viewport(source, top, 200), 180, 20, QColor("#445566"))
            for top in (0, 150, 300)
        ]

        result = stitch(frames)
        sticky = QColor("#445566").rgb()
        rows = [y for y, pixel in enumerate(self._column(result)) if pixel == sticky]

        assert rows == list(range(result.height() - 20, result.height()))

    def test_no_content_is_lost_between_the_joins(self):
        # The failure that matters most and is easiest to miss by eye: a
        # seam that looks clean because a strip was silently dropped.
        source = page(700)
        frames = [viewport(source, top, 200) for top in (0, 120, 240, 360, 480, 500)]

        result = stitch(frames)

        assert self._column(result) == self._column(source)

    def test_frames_that_do_not_overlap_raise_rather_than_guess(self):
        # A full-page capture that silently drops a band is worse than one
        # that says it could not do it -- the user cannot see what is
        # missing from a page they were scrolling past.
        source = page(1200)
        frames = [viewport(source, 0, 200), viewport(source, 900, 200)]

        with pytest.raises(StitchError, match="do not overlap"):
            stitch(frames)

    def test_nothing_to_stitch_raises(self):
        with pytest.raises(StitchError):
            stitch([])

    def test_turning_sticky_removal_off_changes_nothing_without_one(self):
        source = page(500)
        frames = [viewport(source, top, 200) for top in (0, 150, 300)]

        assert self._column(stitch(frames, drop_sticky=False)) == self._column(source)

    def test_a_sticky_page_cannot_be_stitched_with_removal_off(self):
        # Not a limitation worth working around -- it is the same fact the
        # sticky handling exists for, seen from the other side. The rows
        # used to locate a join come from the top of each frame, and on a
        # page with a sticky header those rows are frozen: they match
        # everywhere and therefore nowhere. Raising is right; a join placed
        # on frozen rows would be silently wrong.
        source = page(500)
        frames = [
            band(viewport(source, top, 200), 0, 25, QColor("#112233"))
            for top in (0, 150, 300)
        ]

        with pytest.raises(StitchError):
            stitch(frames, drop_sticky=False)

        assert stitch(frames).height() == 500, "and it stitches fine with it on"
