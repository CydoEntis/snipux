import pytest
from PyQt6.QtCore import QPointF, QRectF, QSizeF
from PyQt6.QtGui import QColor, QFontMetrics, QImage, QPainter, qRgb
from PyQt6.QtWidgets import QApplication

from snipux.capture import Frame
from snipux.shapes import (
    Arrow,
    Blur,
    Crop,
    Ellipse,
    Highlighter,
    Line,
    Pen,
    Pixelate,
    Rectangle,
    StepMarker,
    Text,
    _OBSCURE_DOWNSCALE_DIVISOR,
    apply_crop,
    render,
)

BACKGROUND = qRgb(255, 255, 255)
RED = QColor(255, 0, 0)
BLUE = QColor(0, 0, 255)


@pytest.fixture(scope="module", autouse=True)
def qapp():
    # PyQt6 needs a live QApplication before any QImage/QPainter work, even
    # offscreen, matching the convention in test_editor.py/test_overlay.py.
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def make_image(size=(100, 100), fill_color=BACKGROUND) -> QImage:
    image = QImage(*size, QImage.Format.Format_RGB32)
    image.fill(fill_color)
    return image


def make_gradient_image(size=(80, 80)) -> QImage:
    # A left-to-right red ramp, not a flat fill: Blur/Pixelate over a flat
    # region would look identical (there'd be nothing to average), so
    # telling them apart needs a base image with real per-pixel variation.
    width, height = size
    image = QImage(width, height, QImage.Format.Format_RGB32)
    for x in range(width):
        red = round(255 * x / (width - 1))
        image.setPixelColor(x, 0, QColor(red, 0, 0))
        for y in range(1, height):
            image.setPixelColor(x, y, image.pixelColor(x, 0))
    return image


class TestShapeFields:
    def test_pen_stores_colour_and_stroke_width(self):
        shape = Pen(colour=RED, stroke_width=4, points=[QPointF(0, 0)])
        assert shape.colour == RED
        assert shape.stroke_width == 4

    def test_highlighter_stores_colour_and_stroke_width(self):
        shape = Highlighter(colour=RED, stroke_width=8, points=[QPointF(0, 0)])
        assert shape.colour == RED
        assert shape.stroke_width == 8

    def test_arrow_stores_colour_and_stroke_width(self):
        shape = Arrow(
            colour=RED, stroke_width=2, start=QPointF(0, 0), end=QPointF(10, 10)
        )
        assert shape.colour == RED
        assert shape.stroke_width == 2

    def test_line_stores_colour_and_stroke_width(self):
        shape = Line(
            colour=RED, stroke_width=2, start=QPointF(0, 0), end=QPointF(10, 10)
        )
        assert shape.colour == RED
        assert shape.stroke_width == 2

    def test_rectangle_stores_colour_and_stroke_width(self):
        shape = Rectangle(
            colour=RED, stroke_width=2, start=QPointF(0, 0), end=QPointF(10, 10)
        )
        assert shape.colour == RED
        assert shape.stroke_width == 2

    def test_ellipse_stores_colour_and_stroke_width(self):
        shape = Ellipse(
            colour=RED, stroke_width=2, start=QPointF(0, 0), end=QPointF(10, 10)
        )
        assert shape.colour == RED
        assert shape.stroke_width == 2

    def test_text_stores_colour_stroke_width_and_text(self):
        shape = Text(
            colour=RED, stroke_width=3, point=QPointF(5, 5), text="hello"
        )
        assert shape.colour == RED
        assert shape.stroke_width == 3
        assert shape.text == "hello"

    def test_step_marker_stores_colour_and_stroke_width(self):
        shape = StepMarker(colour=RED, stroke_width=3, point=QPointF(5, 5))
        assert shape.colour == RED
        assert shape.stroke_width == 3
        assert shape.number == 0  # not meaningful until a render() pass


class TestRender:
    def test_does_not_mutate_base_image(self):
        base = make_image()
        original = QImage(base)  # separate copy to compare against after render()
        shape = Rectangle(
            colour=RED, stroke_width=4, start=QPointF(10, 10), end=QPointF(50, 50)
        )

        render(base, [shape])

        assert base == original

    def test_returns_a_new_image_with_the_shape_painted(self):
        base = make_image()
        shape = Rectangle(
            colour=RED, stroke_width=4, start=QPointF(10, 10), end=QPointF(50, 50)
        )

        result = render(base, [shape])

        assert result.pixelColor(10, 30) == RED  # left border

    def test_draws_multiple_shapes_in_list_order(self):
        base = make_image()
        # Stroke well clear of the image edge and thick enough that its
        # centerline pixel is fully covered, not antialiased against the
        # background — a clean "which colour is on top" comparison.
        first = Rectangle(
            colour=RED, stroke_width=6, start=QPointF(10, 10), end=QPointF(70, 70)
        )
        second = Rectangle(
            colour=BLUE, stroke_width=6, start=QPointF(10, 10), end=QPointF(70, 70)
        )

        result = render(base, [first, second])

        # Same rect drawn twice: the later shape in the list wins at the
        # overlapping border pixel, proving draw order is respected.
        assert result.pixelColor(10, 40) == BLUE

    def test_draws_text_and_step_marker_in_list_order(self):
        base = make_image()
        text = Text(colour=RED, stroke_width=4, point=QPointF(10, 50), text="hi")
        marker = StepMarker(colour=BLUE, stroke_width=4, point=QPointF(50, 50))

        result = render(base, [text, marker])

        assert result.pixelColor(50, 50) == BLUE  # marker centre: filled badge
        assert result != base


class TestHighlighterVsPen:
    def test_pen_is_opaque_and_highlighter_blends_with_background(self):
        pen_image = make_image(fill_color=BACKGROUND)
        highlighter_image = make_image(fill_color=BACKGROUND)
        points = [QPointF(10, 50), QPointF(90, 50)]

        pen_result = render(pen_image, [Pen(colour=RED, stroke_width=10, points=points)])
        highlighter_result = render(
            highlighter_image,
            [Highlighter(colour=RED, stroke_width=10, points=points)],
        )

        pen_pixel = pen_result.pixelColor(50, 50)
        highlighter_pixel = highlighter_result.pixelColor(50, 50)

        assert pen_pixel.red() == pytest.approx(255, abs=5)
        assert pen_pixel.green() == pytest.approx(0, abs=5)

        # A blend of RED and white background stays visibly lighter than
        # pure red on every channel that pure red zeroes out.
        assert highlighter_pixel != pen_pixel
        assert highlighter_pixel.green() > pen_pixel.green()
        assert highlighter_pixel.blue() > pen_pixel.blue()


class TestArrowVsLine:
    def test_arrow_has_a_visible_head_line_does_not(self):
        start = QPointF(10, 50)
        end = QPointF(90, 50)
        # Straight shaft runs along y=50; this point sits inside the
        # arrowhead's flare (well above the shaft's own stroke width) but
        # nowhere near the plain line, which never paints anything there.
        probe_x, probe_y = 80, 46

        arrow_image = make_image()
        line_image = make_image()

        arrow_result = render(
            arrow_image, [Arrow(colour=RED, stroke_width=4, start=start, end=end)]
        )
        line_result = render(
            line_image, [Line(colour=RED, stroke_width=4, start=start, end=end)]
        )

        assert arrow_result.pixelColor(probe_x, probe_y) != QColor(BACKGROUND)
        assert line_result.pixelColor(probe_x, probe_y) == QColor(BACKGROUND)


class TestTextRendering:
    def test_empty_text_is_a_no_op(self):
        base = make_image()

        result = render(base, [Text(colour=RED, stroke_width=4, point=QPointF(10, 50), text="")])

        assert result == base

    def test_stroke_width_changes_rendered_glyph_size(self):
        # Regression test for the "setFont was never called" bug PLAN.md
        # flags: without painter.setFont(font), drawText silently uses the
        # painter's default font and every Text renders at one fixed size
        # regardless of stroke width, with no exception to mark the mistake.
        thin = Text(colour=RED, stroke_width=2, point=QPointF(0, 0), text="X")
        thick = Text(colour=RED, stroke_width=20, point=QPointF(0, 0), text="X")

        thin_metrics = QFontMetrics(thin._font())
        thick_metrics = QFontMetrics(thick._font())

        assert thick_metrics.horizontalAdvance("X") > thin_metrics.horizontalAdvance("X")

    def test_draws_the_glyph_at_point_in_the_shapes_colour(self):
        base = make_image()
        point = QPointF(10, 60)
        text = Text(colour=RED, stroke_width=20, point=point, text="X")

        result = render(base, [text])

        # drawText's `point` is the text baseline, so the glyph is painted in
        # a box above and to the right of it, not at `point` itself. Scan
        # that box rather than one exact pixel — antialiasing means the
        # precise glyph shape isn't guaranteed pixel-for-pixel across font
        # backends, but a correct setPen(colour)/setFont(font)/point offset
        # must paint *some* pixel in the box, and in the shape's own colour.
        metrics = QFontMetrics(text._font())
        xs = range(int(point.x()), int(point.x()) + metrics.horizontalAdvance("X"))
        ys = range(int(point.y()) - metrics.ascent(), int(point.y()))
        painted = [result.pixelColor(x, y) for x in xs for y in ys]

        assert any(p != QColor(BACKGROUND) for p in painted)
        assert any(p == RED for p in painted)


class TestStepMarkerRendering:
    def test_renders_a_filled_badge(self):
        base = make_image()
        marker = StepMarker(colour=RED, stroke_width=4, point=QPointF(50, 50))

        result = render(base, [marker])

        assert result.pixelColor(50, 50) == RED


class TestStepMarkerNumbering:
    def test_numbers_markers_sequentially_in_list_order(self):
        base = make_image()
        markers = [
            StepMarker(colour=RED, stroke_width=4, point=QPointF(10, 10)),
            StepMarker(colour=RED, stroke_width=4, point=QPointF(30, 30)),
            StepMarker(colour=RED, stroke_width=4, point=QPointF(50, 50)),
        ]

        render(base, markers)

        assert [marker.number for marker in markers] == [1, 2, 3]

    def test_renumbers_after_an_earlier_marker_is_removed(self):
        base = make_image()
        markers = [
            StepMarker(colour=RED, stroke_width=4, point=QPointF(10, 10)),
            StepMarker(colour=RED, stroke_width=4, point=QPointF(30, 30)),
            StepMarker(colour=RED, stroke_width=4, point=QPointF(50, 50)),
        ]
        render(base, markers)

        survivors = markers[1:]  # drop the first, as if the user removed it
        render(base, survivors)

        assert [marker.number for marker in survivors] == [1, 2]


class TestObscuringShapeFields:
    def test_blur_stores_colour_and_stroke_width(self):
        shape = Blur(colour=RED, stroke_width=3, start=QPointF(0, 0), end=QPointF(10, 10))
        assert shape.colour == RED
        assert shape.stroke_width == 3

    def test_pixelate_stores_colour_and_stroke_width(self):
        shape = Pixelate(colour=RED, stroke_width=3, start=QPointF(0, 0), end=QPointF(10, 10))
        assert shape.colour == RED
        assert shape.stroke_width == 3

    def test_obscuring_shape_draw_raises(self):
        # render() must never call draw() on one of these — it dispatches
        # to apply() instead (see TestBlurOrdering below). This pins that
        # contract so a future regression that reintroduces a draw() call
        # for these shapes fails loudly instead of silently painting
        # nothing.
        shape = Blur(colour=RED, stroke_width=3, start=QPointF(0, 0), end=QPointF(10, 10))
        image = make_image()  # kept alive for the painter's lifetime
        painter = QPainter(image)
        try:
            with pytest.raises(NotImplementedError):
                shape.draw(painter)
        finally:
            painter.end()


class TestBlurOrdering:
    def test_blur_over_a_rectangle_differs_from_blur_over_blank(self):
        # The ordering property this ticket exists for: a Blur later in the
        # shape list must obscure whatever was already drawn, not the
        # untouched base image. Proven by rendering the *same* Blur rect in
        # two lists — one with a Rectangle drawn first, one without — in
        # the same render() call each time, and showing the results differ
        # at a pixel inside the blur.
        rect = Rectangle(
            colour=RED,
            stroke_width=300,  # wildly thick: the "border" fills the whole rect
            start=QPointF(0, 0),
            end=QPointF(100, 100),
        )
        blur = Blur(colour=RED, stroke_width=4, start=QPointF(20, 20), end=QPointF(80, 80))

        with_rectangle = render(make_image(), [rect, blur])
        blur_alone = render(make_image(), [blur])

        probe = (50, 50)
        assert with_rectangle.pixelColor(*probe) != blur_alone.pixelColor(*probe)

    def test_does_not_mutate_base_image(self):
        base = make_gradient_image()
        original = QImage(base)
        blur = Blur(colour=RED, stroke_width=4, start=QPointF(10, 10), end=QPointF(60, 60))

        render(base, [blur])

        assert base == original

    def test_degenerate_rect_is_a_no_op(self):
        # paintEvent re-renders on every mouseMoveEvent during a drag, so
        # apply() must survive being called before the user has dragged
        # anywhere (start == end).
        base = make_image()
        blur = Blur(colour=RED, stroke_width=4, start=QPointF(30, 30), end=QPointF(30, 30))

        result = render(base, [blur])

        assert result == base

    def test_rect_flush_with_image_edge_is_not_clipped(self):
        # Regression case for the clamping guard: a rect edge landing
        # exactly at image.width()/height() (not past it) must still be
        # processed in full, not treated as out-of-bounds.
        base = make_gradient_image(size=(40, 40))
        blur = Blur(colour=RED, stroke_width=4, start=QPointF(0, 0), end=QPointF(40, 40))

        result = render(base, [blur])

        assert result != base  # the whole rect was processed, not skipped
        assert result.pixelColor(39, 39) != base.pixelColor(39, 39)


class TestPixelateBlocky:
    def test_uniform_within_a_block_varies_across_blocks(self):
        size = 80
        base = make_gradient_image(size=(size, size))
        pixelate = Pixelate(
            colour=RED, stroke_width=4, start=QPointF(0, 0), end=QPointF(size, size)
        )

        result = render(base, [pixelate])

        # Block width is derived from the divisor rather than hardcoded, so
        # retuning _OBSCURE_DOWNSCALE_DIVISOR (an implementation detail, see
        # shapes.py) doesn't break this test's premise.
        block_width = size // (size // _OBSCURE_DOWNSCALE_DIVISOR)
        # Two pixels a couple of px apart inside block 0 must match...
        assert result.pixelColor(1, 40) == result.pixelColor(3, 40)
        # ...but a pixel a full block away is not required to, and for this
        # gradient does not.
        assert result.pixelColor(1, 40) != result.pixelColor(block_width + 1, 40)

    def test_pixelate_distinguishable_from_blur_over_same_region(self):
        blur_base = make_gradient_image(size=(80, 80))
        pixelate_base = make_gradient_image(size=(80, 80))
        rect = (QPointF(0, 0), QPointF(80, 80))

        blur_result = render(blur_base, [Blur(colour=RED, stroke_width=4, start=rect[0], end=rect[1])])
        pixelate_result = render(
            pixelate_base, [Pixelate(colour=RED, stroke_width=4, start=rect[0], end=rect[1])]
        )

        assert blur_result != pixelate_result


class TestCropShape:
    def test_crop_stores_colour_and_stroke_width(self):
        shape = Crop(colour=RED, stroke_width=2, start=QPointF(0, 0), end=QPointF(10, 10))
        assert shape.colour == RED
        assert shape.stroke_width == 2

    def test_crop_paints_an_outline_not_a_fill(self):
        base = make_image()
        crop = Crop(colour=RED, stroke_width=4, start=QPointF(10, 10), end=QPointF(70, 70))

        result = render(base, [crop])

        # A dashed line has gaps, so scan the left border for *any* painted
        # pixel rather than asserting one exact point is RED.
        left_border = [result.pixelColor(10, y) for y in range(10, 71)]
        assert any(colour == RED for colour in left_border)
        assert result.pixelColor(40, 40) == QColor(BACKGROUND)  # interior: untouched


class TestApplyCrop:
    def _frame(self, image_size=(100, 100), logical_size=None, fill_color=BACKGROUND):
        image = make_image(size=image_size, fill_color=fill_color)
        logical_size = logical_size or QSizeF(*image_size)
        return Frame(image=image, logical_origin=QPointF(0, 0), logical_size=logical_size)

    def test_restricts_image_to_the_crop_rect(self):
        frame = self._frame(image_size=(100, 100))
        crop_rect = QRectF(10, 10, 40, 30)

        result = apply_crop(frame, [], crop_rect)

        assert result.image.width() == 40
        assert result.image.height() == 30

    def test_bakes_in_annotations_before_cropping(self):
        frame = self._frame(image_size=(100, 100))
        # A rectangle straddling the crop boundary, thick enough to paint
        # solidly at (25, 25) crop-relative == (35, 35) image-absolute.
        rect = Rectangle(
            colour=RED, stroke_width=100, start=QPointF(20, 20), end=QPointF(50, 50)
        )
        crop_rect = QRectF(10, 10, 40, 40)

        result = apply_crop(frame, [rect], crop_rect)

        assert result.image.pixelColor(25, 25) == RED

    def test_logical_geometry_scales_with_the_crop(self):
        # scale_x == scale_y == 2: image pixels are twice logical units.
        frame = self._frame(image_size=(200, 150), logical_size=QSizeF(100, 75))
        frame.logical_origin = QPointF(5, 5)
        crop_rect = QRectF(20, 20, 60, 40)

        result = apply_crop(frame, [], crop_rect)

        assert result.logical_origin == QPointF(5 + 10, 5 + 10)
        assert result.logical_size == QSizeF(30, 20)

    def test_zero_area_crop_produces_an_empty_image(self):
        # Canvas is expected to guard against this before calling
        # apply_crop (see test_editor.py), but apply_crop itself must not
        # raise if it's ever called with a degenerate rect.
        frame = self._frame(image_size=(100, 100))
        crop_rect = QRectF(10, 10, 0, 0)

        result = apply_crop(frame, [], crop_rect)

        assert result.image.width() == 0
        assert result.image.height() == 0
