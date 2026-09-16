"""The glass under the bars and menus (#70): a blurred crop of the frozen
frame behind each surface, cached per placement, and the fallback where no
blur can be had.

Every widget size and position here is logical, and every crop is compared
in the frame's own pixels. A frame carries the screen's device pixel ratio
worth of pixels per logical one, as a real capture does, so under
QT_SCALE_FACTOR=1.5 a crop taken in the wrong space comes out offset rather
than coincidentally right. Where a test pins its own ratio, it says so.

`grab()` returns physical pixels: sample it through `_pixel`, and measure
widgets with `grab().deviceIndependentSize()`.

A host window must be held in a variable for as long as its surfaces are
used: collected, it takes every child with it.
"""

from types import SimpleNamespace

import pytest
from PyQt6.QtCore import QMargins, QPoint, QPointF, QRect, QRectF, QSizeF, Qt
from PyQt6.QtGui import QColor, QImage, QPainter
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QGraphicsOpacityEffect, QWidget

import snipux.overlay as overlay_module
from snipux import glass
from snipux.capture import Frame
from snipux.chooser import ChooserRow
from snipux.design import bar_color, tokens
from snipux.marks import ToolStyles
from snipux.overlay import FamilyMenu, FloatingBar, OverlayWindow, StylePopover, WatermarkMenu
from snipux.review import ReviewWindow


@pytest.fixture(scope="module", autouse=True)
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


@pytest.fixture(autouse=True)
def _no_windows_left_behind():
    # An open popup takes every mouse event in the process, and a shown
    # overlay stays on top; either would reach into the next test.
    yield
    for widget in QApplication.topLevelWidgets():
        if widget.isVisible():
            widget.close()


@pytest.fixture
def blurs(monkeypatch):
    """Every blur the glass asks for, as (width, height, radius), with the
    blur itself replaced by the crop unchanged -- so a cached crop can be
    compared pixel for pixel with the frame under the widget."""
    calls = []

    def unblurred(image, radius):
        calls.append((image.width(), image.height(), radius))
        return image

    monkeypatch.setattr(glass, "blur", unblurred)
    return calls


@pytest.fixture
def clock(monkeypatch):
    """The glass's clock, held still until a test moves it on."""
    now = [1000.0]
    monkeypatch.setattr(glass, "_now", lambda: now[0])
    return now


def _ratio() -> float:
    return QApplication.primaryScreen().devicePixelRatio()


def coded_image(width: int, height: int) -> QImage:
    """An image in which no two pixels are alike: blue and green are x's and
    y's low bytes, red their high nibbles. A crop off by one pixel anywhere
    differs from the right one."""
    reds = {
        high: bytes((((x >> 8) & 0x0F) << 4) | high for x in range(width))
        for high in range(((height - 1) >> 8) + 1)
    }
    row = bytearray(width * 4)
    row[0::4] = bytes(x & 0xFF for x in range(width))
    row[3::4] = b"\xff" * width
    data = bytearray()
    for y in range(height):
        row[1::4] = bytes((y & 0xFF,)) * width
        row[2::4] = reds[y >> 8]
        data += row
    buffer = bytes(data)
    return QImage(buffer, width, height, QImage.Format.Format_RGB32).copy()


def _frame(image: QImage, logical_size, origin) -> Frame:
    return Frame(image=image, logical_origin=QPointF(*origin), logical_size=QSizeF(*logical_size))


def coded_frame(logical_size, origin=(0, 0), ratio=None) -> Frame:
    ratio = _ratio() if ratio is None else ratio
    width, height = logical_size
    return _frame(coded_image(round(width * ratio), round(height * ratio)), logical_size, origin)


def flat_frame(logical_size, colour: QColor, origin=(0, 0)) -> Frame:
    width, height = logical_size
    image = QImage(round(width * _ratio()), round(height * _ratio()), QImage.Format.Format_RGB32)
    image.fill(colour)
    return _frame(image, logical_size, origin)


class _Host(QWidget):
    """A window that paints `frame` edge to edge from the frame's own origin,
    as `OverlayWindow` does -- the contract a host keeps, without the rest of
    the overlay."""

    def __init__(self, frame: Frame):
        super().__init__(None, Qt.WindowType.FramelessWindowHint)
        self.frame = frame
        self.setGeometry(
            round(frame.logical_origin.x()), round(frame.logical_origin.y()),
            round(frame.logical_size.width()), round(frame.logical_size.height()),
        )

    def glass_backdrop(self):
        return self.frame.image, QRectF(self.rect())


def _absolute(child: QWidget, frame: Frame) -> QRectF:
    """A direct child's rect, from its host window's local logical space to
    the absolute logical space `Frame` is in."""
    return QRectF(child.geometry()).translated(frame.logical_origin)


def _same_pixels(a: QImage, b: QImage) -> bool:
    return a.convertToFormat(QImage.Format.Format_RGB32) == b.convertToFormat(QImage.Format.Format_RGB32)


def _pixel(image: QImage, x: float, y: float) -> QColor:
    """`image` at a logical point: `grab()` is physical."""
    ratio = image.devicePixelRatio()
    return image.pixelColor(round(x * ratio), round(y * ratio))


def _over(fill: QColor, ground: QColor) -> tuple:
    alpha = fill.alphaF()
    return tuple(
        round(f * alpha + g * (1 - alpha))
        for f, g in zip(fill.getRgb()[:3], ground.getRgb()[:3])
    )


def _bar(host: QWidget | None = None, at=(100, 100)) -> FloatingBar:
    bar = FloatingBar(host)
    bar.resize(bar.sizeHint())
    bar.move(*at)
    return bar


def _family_menu(host: QWidget | None = None, at=(100, 100)) -> FamilyMenu:
    menu = FamilyMenu("shapes", host)
    menu.resize(menu.sizeHint())
    menu.move(*at)
    return menu


def _watermark_menu(host: QWidget | None = None, at=(100, 100)) -> WatermarkMenu:
    # Its width is fixed; its height follows its rows.
    menu = WatermarkMenu(host)
    menu.resize(menu.width(), menu.sizeHint().height())
    menu.move(*at)
    return menu


def _style_popover(host: QWidget | None = None, at=(100, 100)) -> StylePopover:
    popover = StylePopover(ToolStyles(), host)
    popover.set_tool("pen")
    popover.resize(popover.width(), popover.sizeHint().height())
    popover.move(*at)
    return popover


FALLBACK = round(tokens.BarColor.FALLBACK_BG_ALPHA * 255)

# The bar and a menu off it: what the cache and the fallback are checked on.
BAR_AND_WATERMARK_MENU = [
    pytest.param(_bar, tokens.BarColor.BAR_BG_ALPHA, id="stills bar"),
    pytest.param(_watermark_menu, tokens.BarColor.MENU_BG_ALPHA, id="watermark menu"),
]


class TestTheCropIsTheFrameUnderTheWidget:
    def test_a_surface_on_its_host(self, blurs):
        frame = coded_frame((800, 500))
        host = _Host(frame)
        bar = _bar(host, at=(171, 309))

        bar.grab()

        assert _same_pixels(bar.glass.backdrop(), frame.crop(_absolute(bar, frame)).image)
        ratio = _ratio()
        assert bar.glass.crop_rect.topLeft() == QPoint(round(171 * ratio), round(309 * ratio))

    @pytest.mark.parametrize(
        "build", [pytest.param(_family_menu, id="family menu"), pytest.param(_watermark_menu, id="watermark menu")]
    )
    def test_the_frames_own_pixels_decide_the_scale_not_the_screens(self, blurs, build):
        # Two frame pixels to a logical one, whatever QT_SCALE_FACTOR says.
        frame = coded_frame((600, 400), ratio=2)
        host = _Host(frame)
        menu = build(host, at=(123, 77))

        menu.grab()

        assert menu.glass.crop_rect == QRect(246, 154, 2 * menu.width(), 2 * menu.height())
        assert _same_pixels(menu.glass.backdrop(), frame.crop(_absolute(menu, frame)).image)

    @pytest.mark.parametrize(
        "build", [pytest.param(_style_popover, id="style popover"), pytest.param(_watermark_menu, id="watermark menu")]
    )
    def test_on_a_monitor_with_a_negative_origin(self, blurs, build):
        # The host's origin is the top-left of a monitor left of and above
        # the primary, so a surface near it is at negative absolute
        # coordinates.
        frame = coded_frame((1040, 600), origin=(-400, -120))
        host = _Host(frame)
        surface = build(host, at=(30, 40))
        absolute = _absolute(surface, frame)
        assert absolute.left() < 0 and absolute.top() < 0

        surface.grab()

        assert _same_pixels(surface.glass.backdrop(), frame.crop(absolute).image)

    def test_the_blur_radius_is_the_tokens_in_frame_pixels(self, blurs):
        host = _Host(coded_frame((600, 400), ratio=2))
        _bar(host).grab()

        (_width, _height, radius), = blurs
        assert radius == tokens.BarMetric.BACKDROP_BLUR * 2


class TestTheOverlayIsTheHost:
    """The real window, on a desk with a monitor left of and above the
    primary. Every surface over it -- children of the window and windows of
    their own -- is cropped from where it is, not from the selection's
    monitor or from the desktop's origin."""

    # 700 wide, not 600: the destination menu centres on the bar's own
    # split-action button with no clamp of its own (`FlowMenu.open_below`),
    # so the room it needs comes entirely from how far right the bar's own
    # clamp (against this monitor's right edge) lets the bar sit. 600 was
    # tight enough before the eyedropper's slot widened the bar by one
    # icon that the menu already touched this monitor's left edge with
    # nothing to spare; 700 restores the same margin the handoff intended.
    LEFT = QRectF(-600, -200, 700, 400)
    PRIMARY = QRectF(0, 0, 800, 500)

    @pytest.fixture(autouse=True)
    def _desk(self, monkeypatch, blurs):
        monkeypatch.setattr(
            overlay_module.platform.current, "reserved_margins", lambda screen: QMargins()
        )
        # The pointer is on the left monitor as far as the OS knows, so the
        # row opens there.
        monkeypatch.setattr(overlay_module, "QCursor", SimpleNamespace(pos=lambda: QPoint(-300, 0)))
        self.frame = coded_frame((1400, 700), origin=(-600, -200))
        self.overlay = OverlayWindow(self.frame, monitor_geometries=[self.LEFT, self.PRIMARY])
        self.overlay.show()
        QTest.qWaitForWindowExposed(self.overlay)
        yield
        self.overlay.close()

    def _assert_cropped_from(self, surface: QWidget, absolute: QRectF) -> None:
        assert surface.isVisible()
        assert QRectF(self.frame.logical_origin, self.frame.logical_size).contains(absolute)
        surface.grab()
        assert _same_pixels(surface.glass.backdrop(), self.frame.crop(absolute).image)

    def _select_on_the_left_monitor(self) -> QRect:
        # Window-local; absolute (-300, -100) to (-50, 50). Far enough from
        # the desk's left edge that the menus opening off the bar's left end
        # stay on the frame.
        selection = QRect(300, 100, 250, 150)
        self.overlay.set_selection(selection)
        return selection

    def test_the_row_and_its_hint(self):
        chooser = self.overlay._chooser
        for surface in (chooser.row, chooser.hint):
            absolute = _absolute(surface, self.frame)
            assert self.LEFT.contains(absolute.center())
            self._assert_cropped_from(surface, absolute)

    def test_the_mode_menu_a_window_of_its_own(self):
        chooser = self.overlay._chooser
        chooser._toggle_menu()
        menu = chooser._menu
        assert menu.isWindow()

        # A window's geometry is global, which for this overlay is the
        # absolute logical space the frame is in.
        self._assert_cropped_from(menu, QRectF(menu.geometry()))

    def test_the_bar_the_tab_and_the_tool_hint(self):
        self._select_on_the_left_monitor()
        overlay = self.overlay

        for surface in (overlay._bar, overlay._chooser.tab, overlay._tool_hint):
            absolute = _absolute(surface, self.frame)
            assert self.LEFT.contains(absolute.center())
            self._assert_cropped_from(surface, absolute)

    def test_the_menus_off_the_bar(self):
        self._select_on_the_left_monitor()
        overlay = self.overlay

        overlay._toggle_family_menu("shapes")
        menu = overlay._family_menus["shapes"]
        self._assert_cropped_from(menu, _absolute(menu, self.frame))

        overlay._toggle_style()
        popover = overlay._style_popover
        self._assert_cropped_from(popover, _absolute(popover, self.frame))

        # Opened straight from the notch's handler: with nothing in Settings
        # the slot is greyed, but the menu is still the bar's menu.
        overlay._toggle_watermark_menu()
        watermark = overlay._watermark_menu
        self._assert_cropped_from(watermark, _absolute(watermark, self.frame))

    def test_the_destination_menu_a_window_with_no_parent(self):
        self._select_on_the_left_monitor()

        self.overlay._open_destination_menu()
        menu = self.overlay._destination_menu

        assert menu.parentWidget() is None
        self._assert_cropped_from(menu, QRectF(menu.geometry()))

    def test_no_surface_is_made_translucent_as_a_whole(self):
        # Alpha is not opacity. The collapsed tab's 70% is the one real
        # opacity among the surfaces; the dimmed style dot is not a surface.
        self._select_on_the_left_monitor()
        overlay = self.overlay
        overlay._toggle_style()
        chooser = overlay._chooser
        surfaces = [
            overlay._bar, overlay._tool_hint, overlay._style_popover, overlay._popover,
            overlay._watermark_menu, *overlay._family_menus.values(), chooser.row, chooser.hint,
        ]

        for surface in surfaces:
            assert surface.windowOpacity() == 1.0
            assert not isinstance(surface.graphicsEffect(), QGraphicsOpacityEffect)
        assert chooser.tab.windowOpacity() == 1.0
        assert chooser.tab.opacity() == pytest.approx(tokens.BarMetric.TAB_OPACITY)

    def test_exports_are_the_frame_under_the_selection_whatever_glass_is_over_it(self):
        before = self.frame.image.copy()
        selection = self._select_on_the_left_monitor()
        overlay = self.overlay
        overlay._toggle_style()
        for surface in (overlay._bar, overlay._style_popover):
            surface.grab()
        assert overlay._bar.glass.backdrop() is not None

        exported = overlay.rendered_image()

        expected = self.frame.crop(QRectF(selection).translated(self.frame.logical_origin)).image
        assert _same_pixels(exported, expected)
        # Cropping and blurring never write back into the frame.
        assert self.frame.image == before


class TestTheCropIsCached:
    @pytest.mark.parametrize("build, _alpha", BAR_AND_WATERMARK_MENU)
    def test_a_repaint_reuses_it(self, blurs, clock, build, _alpha):
        host = _Host(coded_frame((800, 500)))
        surface = build(host)
        surface.grab()
        first = surface.glass.backdrop()

        surface.update()
        surface.grab()
        surface.grab()

        assert len(blurs) == 1
        assert surface.glass.backdrop().cacheKey() == first.cacheKey()

    @pytest.mark.parametrize("build, _alpha", BAR_AND_WATERMARK_MENU)
    def test_a_moved_widget_recomputes_it(self, blurs, clock, build, _alpha):
        frame = coded_frame((800, 500))
        host = _Host(frame)
        surface = build(host)
        surface.grab()

        clock[0] += 10
        surface.move(250, 180)
        surface.grab()

        assert len(blurs) == 2
        assert _same_pixels(surface.glass.backdrop(), frame.crop(_absolute(surface, frame)).image)

    def test_a_new_frame_recomputes_it(self, blurs, clock):
        host = _Host(coded_frame((800, 500)))
        bar = _bar(host)
        bar.grab()

        clock[0] += 10
        host.frame = flat_frame((800, 500), QColor("#336699"))
        bar.grab()

        assert len(blurs) == 2
        assert _same_pixels(bar.glass.backdrop(), host.frame.crop(_absolute(bar, host.frame)).image)

    def test_while_it_moves_the_last_crop_stands_in_until_it_rests(self, blurs, clock):
        frame = coded_frame((800, 500))
        host = _Host(frame)
        bar = _bar(host, at=(10, 10))
        bar.grab()

        # Setting off is a move away from rest: cropped straight away.
        clock[0] += 10
        bar.move(20, 20)
        bar.grab()
        set_off = bar.glass.backdrop()
        assert len(blurs) == 2

        # A drag: each move follows the last within SETTLE_MS.
        for step in (30, 40, 50):
            clock[0] += 0.02
            bar.move(step, step)
            bar.grab()
        assert len(blurs) == 2
        assert bar.glass.backdrop().cacheKey() == set_off.cacheKey()

        # At rest.
        clock[0] += glass.SETTLE_MS / 1000 + 0.01
        bar.grab()
        assert len(blurs) == 3
        assert _same_pixels(bar.glass.backdrop(), frame.crop(_absolute(bar, frame)).image)


class TestWhereNoBlurCanBeHad:
    def test_with_nothing_behind_it_the_fill_rises_to_the_fallback_alpha(self):
        bar = FloatingBar()
        bar.resize(bar.sizeHint())

        sampled = _pixel(bar.grab().toImage(), bar.width() / 2, 2)

        assert sampled.alpha() == pytest.approx(FALLBACK, abs=2)
        # Nothing else changes: the same colour, only denser.
        assert sampled.getRgb()[:3] == QColor(tokens.BarColor.BAR_BG).getRgb()[:3]

    @pytest.mark.parametrize("build, design_alpha", BAR_AND_WATERMARK_MENU)
    def test_the_hook_forces_it_over_a_frame(self, monkeypatch, blurs, build, design_alpha):
        monkeypatch.setattr(glass, "force_fallback", True)
        host = _Host(coded_frame((800, 500)))
        surface = build(host)

        sampled = _pixel(surface.grab().toImage(), surface.width() / 2, 2)

        assert blurs == []
        assert surface.glass.backdrop() is None
        expected = max(design_alpha, tokens.BarColor.FALLBACK_BG_ALPHA)
        assert sampled.alpha() == pytest.approx(round(expected * 255), abs=2)

    def test_off_the_frame(self, blurs):
        host = _Host(coded_frame((400, 300)))
        bar = _bar(host, at=(900, 900))

        sampled = _pixel(bar.grab().toImage(), bar.width() / 2, 2)

        assert blurs == []
        assert sampled.alpha() == pytest.approx(FALLBACK, abs=2)

    @pytest.mark.parametrize(
        "build", [pytest.param(_family_menu, id="family menu"), pytest.param(_watermark_menu, id="watermark menu")]
    )
    def test_a_fill_already_denser_keeps_its_own_alpha(self, build):
        menu = build()

        sampled = _pixel(menu.grab().toImage(), menu.width() / 2, 2)

        assert tokens.BarColor.MENU_BG_ALPHA > tokens.BarColor.FALLBACK_BG_ALPHA
        assert sampled.alpha() == pytest.approx(round(tokens.BarColor.MENU_BG_ALPHA * 255), abs=2)


class TestTheGround:
    """The real blur, not the stand-in."""

    GROUND = QColor(200, 120, 40)

    def test_is_the_fill_over_the_blurred_frame(self):
        # A flat frame blurs to itself, so the ground is exactly the design's
        # fill over it -- and opaque, where the fallback is not.
        host = _Host(flat_frame((800, 500), self.GROUND))
        bar = _bar(host)
        row = ChooserRow(host)
        # Its shadow would darken the ground under a translucent fill.
        row.graphicsEffect().setEnabled(False)
        row.move(300, 0)
        watermark = _watermark_menu(host, at=(500, 200))

        for surface, fill in ((bar, "BAR_BG"), (row, "BAR_BG"), (watermark, "MENU_BG")):
            expected = _over(bar_color(fill), self.GROUND)
            sampled = _pixel(surface.grab().toImage(), surface.width() / 2, 2)
            assert sampled.alpha() == 255
            for channel, want in zip(sampled.getRgb()[:3], expected):
                assert channel == pytest.approx(want, abs=3)

    def test_what_is_behind_is_blurred(self):
        # Black left of x = 400, white right of it, and a bar across the edge.
        frame = flat_frame((800, 500), QColor("black"))
        painter = QPainter(frame.image)
        painter.fillRect(
            QRect(round(400 * _ratio()), 0, frame.image.width(), frame.image.height()), QColor("white")
        )
        painter.end()
        host = _Host(frame)
        bar = _bar(host, at=(200, 100))
        bar.grab()

        crop = bar.glass.backdrop()
        middle = crop.height() // 2
        edge = round(200 * _ratio())
        just_left = crop.pixelColor(edge - 2, middle).lightness()
        just_right = crop.pixelColor(edge + 1, middle).lightness()
        assert 0 < just_left < just_right < 255
        # Far from the edge, the blur is what the frame is.
        assert crop.pixelColor(0, middle).lightness() <= 2
        assert crop.pixelColor(crop.width() - 1, middle).lightness() >= 253

    def test_does_not_thin_at_the_edge_of_the_frame(self):
        # The chooser row hangs from the frame's top edge. Blurred with
        # nothing past that edge, the crop would fade towards transparent
        # along it.
        host = _Host(flat_frame((800, 500), self.GROUND))
        top_left = _bar(host, at=(0, 0))
        bottom_right = _bar(host)
        bottom_right.move(host.width() - bottom_right.width(), host.height() - bottom_right.height())

        corners = []
        for bar in (top_left, bottom_right):
            bar.grab()
            crop = bar.glass.backdrop()
            corners += [crop.pixelColor(0, 0), crop.pixelColor(crop.width() - 1, crop.height() - 1)]

        for colour in corners:
            assert colour.alpha() == 255
            for channel, want in zip(colour.getRgb()[:3], self.GROUND.getRgb()[:3]):
                assert channel == pytest.approx(want, abs=2)


class TestTheReviewWindowKeepsItsFill:
    """Its bar is over the review canvas, which zooms and takes ink under
    it, not over a frozen frame: the design's fill, with no blur and no
    fallback."""

    def test_its_bar_is_the_tokens_fill_with_nothing_blurred(self, blurs):
        image = QImage(800, 600, QImage.Format.Format_RGB32)
        image.fill(0x336699)
        window = ReviewWindow(image)
        window._set_annotating(True)
        bar = window._bar

        sampled = _pixel(bar.grab().toImage(), bar.width() / 2, 2)

        assert blurs == []
        assert bar.glass.backdrop() is None
        assert sampled.alpha() == pytest.approx(round(tokens.BarColor.BAR_BG_ALPHA * 255), abs=2)
