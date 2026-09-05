"""The scroll driver (SNX-2).

The loop is driven entirely through injected `grab`/`scroll`/`settle`, so
nothing here needs a browser, a desktop or a real mouse -- and nothing can
pass or fail by what happens to be open on the machine running it.
"""

import pytest
from PyQt6.QtGui import QColor, QImage, QPainter
from PyQt6.QtWidgets import QApplication

from snipux.scroll import MAX_FRAMES, ScrollCaptureError, capture_page
from snipux.stitch import PROBE_ROWS


@pytest.fixture(scope="module", autouse=True)
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


WIDTH, VIEW = 40, 200


def tall_page(height: int) -> QImage:
    """A page where every row is a distinct colour, so a misplaced join is
    an assertion failure rather than something to squint at.
    """
    image = QImage(WIDTH, height, QImage.Format.Format_RGB32)
    painter = QPainter(image)
    for y in range(height):
        painter.fillRect(0, y, WIDTH, 1, QColor(y & 0xFF, (y >> 8) & 0xFF, 0x40))
    painter.end()
    return image


class FakeBrowser:
    """A page that scrolls, stops at its own bottom, and can be told to lie
    about how far a scroll went -- which real browsers do constantly.
    """

    def __init__(self, page: QImage, step: int = 150, view: int = VIEW):
        self.page, self.step, self.view = page, step, view
        self.top = 0
        self.grabs = 0
        self.scrolls = 0

    def grab(self) -> QImage:
        self.grabs += 1
        return self.page.copy(0, self.top, self.page.width(), self.view)

    def scroll(self) -> None:
        self.scrolls += 1
        self.top = min(self.top + self.step, self.page.height() - self.view)

    def settle(self) -> None:
        pass


def column(image: QImage) -> list[int]:
    return [image.pixel(WIDTH // 2, y) for y in range(image.height())]


class TestCapturePage:
    def test_it_scrolls_to_the_bottom_and_returns_the_whole_page(self):
        page = tall_page(800)
        browser = FakeBrowser(page)

        result = capture_page(browser.grab, browser.scroll, browser.settle)

        assert result.height() == 800
        assert column(result) == column(page)

    def test_a_page_that_fits_needs_no_scrolling(self):
        # One screenful must come back as exactly itself, not as a
        # re-derived approximation.
        page = tall_page(VIEW)
        browser = FakeBrowser(page)

        result = capture_page(browser.grab, browser.scroll, browser.settle)

        assert result.size() == page.size()
        assert column(result) == column(page)

    def test_the_bottom_is_detected_not_predicted(self):
        # How far a notch scrolls depends on the user's mouse settings, the
        # page's smooth scrolling and its zoom -- so the number of scrolls
        # a page needs cannot be known before scrolling it.
        page = tall_page(1000)
        browser = FakeBrowser(page, step=90)

        result = capture_page(browser.grab, browser.scroll, browser.settle)

        assert result.height() == 1000

    def test_an_uneven_scroll_is_measured_not_assumed(self):
        # A real browser does not move the same distance every time.
        # Every step leaves more than PROBE_ROWS of overlap, which is the
        # real constraint on a scroll driver -- see the next test.
        page = tall_page(900)
        browser = FakeBrowser(page)
        steps = iter([130, 170, 110, 160, 150, 150, 150, 150, 150, 150])

        def wobbly():
            browser.step = next(steps, 150)
            browser.scroll()

        result = capture_page(browser.grab, wobbly, browser.settle)

        assert column(result) == column(page)

    def test_one_unchanged_grab_is_not_the_bottom(self):
        # A scroll that has not finished painting looks exactly like a page
        # that will not move again -- so one still grab must not end it.
        page = tall_page(800)
        browser = FakeBrowser(page)
        real_scroll = browser.scroll
        calls = {"n": 0}

        def stutters():
            calls["n"] += 1
            if calls["n"] == 2:
                return  # this one "did not land"
            real_scroll()

        result = capture_page(browser.grab, stutters, browser.settle)

        assert result.height() == 800

    def test_an_endless_page_stops_and_says_so(self):
        # An infinite feed has no bottom. Scrolling one forever is worse
        # than stopping.
        endless = tall_page(20000)
        browser = FakeBrowser(endless, step=150)

        with pytest.raises(ScrollCaptureError, match="still growing"):
            capture_page(browser.grab, browser.scroll, browser.settle)

    def test_it_gives_up_within_the_frame_budget(self):
        endless = tall_page(20000)
        browser = FakeBrowser(endless, step=150)

        with pytest.raises(ScrollCaptureError):
            capture_page(browser.grab, browser.scroll, browser.settle)

        assert browser.scrolls <= MAX_FRAMES + 2

    def test_an_empty_grab_is_refused_rather_than_stitched(self):
        with pytest.raises(ScrollCaptureError, match="nothing to capture"):
            capture_page(lambda: QImage(), lambda: None, lambda: None)

    def test_an_empty_grab_mid_scroll_is_refused(self):
        page = tall_page(800)
        browser = FakeBrowser(page)
        grabs = {"n": 0}

        def flaky():
            grabs["n"] += 1
            return QImage() if grabs["n"] == 3 else browser.grab()

        with pytest.raises(ScrollCaptureError, match="empty mid-scroll"):
            capture_page(flaky, browser.scroll, browser.settle)

    def test_frames_that_cannot_be_joined_raise_rather_than_guess(self):
        # Scrolling further than a viewport leaves no overlap, and overlap
        # is the only evidence two frames belong together. A partial image
        # with a band missing is worse than a failure: the user cannot see
        # what is absent.
        page = tall_page(2000)
        browser = FakeBrowser(page, step=VIEW + 50)

        with pytest.raises(ScrollCaptureError):
            capture_page(browser.grab, browser.scroll, browser.settle)

    def test_a_scroll_must_leave_more_overlap_than_the_probe_needs(self):
        # The real constraint on how far a driver may scroll, and it is
        # tighter than "less than a viewport": the join is found by
        # matching a run of PROBE_ROWS rows, so a scroll leaving fewer than
        # that has nothing to match against even though the frames do
        # genuinely overlap.
        page = tall_page(1200)
        barely = FakeBrowser(page, step=VIEW - (PROBE_ROWS // 2))

        with pytest.raises(ScrollCaptureError):
            capture_page(barely.grab, barely.scroll, barely.settle)

        roomy = FakeBrowser(page, step=VIEW - PROBE_ROWS * 3)
        assert capture_page(roomy.grab, roomy.scroll, roomy.settle).height() == 1200

    def test_settle_is_waited_on_after_every_scroll(self):
        page = tall_page(700)
        browser = FakeBrowser(page)
        waits = {"n": 0}

        def settle():
            waits["n"] += 1

        capture_page(browser.grab, browser.scroll, settle)

        assert waits["n"] == browser.scrolls
