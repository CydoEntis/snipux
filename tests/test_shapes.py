import pytest
from PyQt6.QtCore import Qt, QPointF, QRectF, QSizeF
from PyQt6.QtGui import QColor, QFontMetrics, QFontMetricsF, QImage, QPainter, qRgb
from PyQt6.QtWidgets import QApplication

from snipux.capture import Frame
from snipux.design.tokens import Color, Font, Metric
from snipux.shapes import (
    Arrow,
    Blur,
    Crop,
    DROP_THRESHOLD,
    Ellipse,
    Highlighter,
    Line,
    Pen,
    Pixelate,
    Rectangle,
    StepMarker,
    Text,
    apply_crop,
    finalize_mark,
    next_step_number,
    render,
    render_selection,
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
        assert shape.number == 0  # not meaningful until assigned by next_step_number()


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


class TestHighlighterGeometry:
    def test_pen_uses_round_caps_and_joins(self):
        shape = Highlighter(colour=RED, stroke_width=8, points=[QPointF(0, 0)])
        pen = shape._pen()
        assert pen.capStyle() == Qt.PenCapStyle.RoundCap
        assert pen.joinStyle() == Qt.PenJoinStyle.RoundJoin

    def test_strokes_at_the_stroke_width_times_tokens_multiplier(self):
        base = make_image()
        stroke_width = 4
        points = [QPointF(10, 50), QPointF(90, 50)]

        result = render(
            base, [Highlighter(colour=RED, stroke_width=stroke_width, points=points)]
        )

        # Band half-width per Metric.HIGHLIGHT_MULT: just inside it must be
        # painted, just outside it must still be the untouched background --
        # pinning the *token-derived* width rather than the old fixed one.
        half_width = stroke_width * Metric.HIGHLIGHT_MULT / 2
        inside_y = 50 - int(half_width) + 1
        outside_y = 50 - int(half_width) - 3

        assert result.pixelColor(50, inside_y) != QColor(BACKGROUND)
        assert result.pixelColor(50, outside_y) == QColor(BACKGROUND)

    def test_blends_at_tokens_alpha(self):
        base = make_image(fill_color=BACKGROUND)
        points = [QPointF(10, 50), QPointF(90, 50)]

        result = render(base, [Highlighter(colour=RED, stroke_width=4, points=points)])

        # RED over white background at Metric.HIGHLIGHT_ALPHA: the
        # background-only channels settle at 255 * (1 - alpha).
        expected_green = round(255 * (1 - Metric.HIGHLIGHT_ALPHA))
        assert result.pixelColor(50, 50).green() == pytest.approx(expected_green, abs=5)


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


class TestArrowHeadGeometry:
    def test_head_size_scales_with_stroke_width(self):
        start = QPointF(10, 50)
        end = QPointF(90, 50)
        # Inside the thick arrow's head flare, but outside both the thin
        # arrow's (much smaller) head and its hairline shaft.
        probe_x, probe_y = 60, 60

        thin_result = render(
            make_image(), [Arrow(colour=RED, stroke_width=1, start=start, end=end)]
        )
        thick_result = render(
            make_image(), [Arrow(colour=RED, stroke_width=10, start=start, end=end)]
        )

        assert thin_result.pixelColor(probe_x, probe_y) == QColor(BACKGROUND)
        assert thick_result.pixelColor(probe_x, probe_y) != QColor(BACKGROUND)

    def test_shaft_stops_short_and_does_not_poke_through_the_tip(self):
        base = make_image()
        # A thick shaft: if drawn all the way to `end` (rather than
        # stopping short by SHAFT_STOP_FRACTION of the head length), the
        # pen's own round cap would bulge out past the head's own apex --
        # which sits exactly at `end`, an infinitesimal point with no
        # rasterized width of its own.
        arrow = Arrow(colour=RED, stroke_width=10, start=QPointF(10, 50), end=QPointF(90, 50))

        result = render(base, [arrow])

        assert result.pixelColor(93, 50) == QColor(BACKGROUND)


class TestTextRendering:
    def test_empty_text_is_a_no_op(self):
        base = make_image()

        result = render(base, [Text(colour=RED, stroke_width=4, point=QPointF(10, 50), text="")])

        assert result == base

    def test_font_size_uses_the_tokens_formula(self):
        # max(TEXT_FONT_SIZE_MIN, stroke_width * TEXT_FONT_SIZE_FACTOR), per
        # docs/design/overlay-redesign.md's "Drawing": "Font size max(12,
        # stroke x 3)" -- this is a different floor/factor than the
        # pre-redesign shared helper StepMarker also used to use.
        floored = Text(colour=RED, stroke_width=1, point=QPointF(0, 0), text="X")
        scaled = Text(colour=RED, stroke_width=10, point=QPointF(0, 0), text="X")

        assert floored._font().pixelSize() == Text.TEXT_FONT_SIZE_MIN
        assert scaled._font().pixelSize() == round(10 * Text.TEXT_FONT_SIZE_FACTOR)

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

    def test_draws_the_glyph_inside_the_chip_in_the_shapes_colour(self):
        # `point` is the chip's top-left corner (image-pixel space), not a
        # text baseline -- the pre-redesign version of this class drew
        # straight onto `point`, with no background chip at all.
        base = make_image()
        point = QPointF(10, 10)
        text = Text(colour=RED, stroke_width=20, point=point, text="X")

        result = render(base, [text])

        metrics = QFontMetricsF(text._font())
        pad_h = Metric.TEXT_LABEL_PAD_H
        pad_v = Metric.TEXT_LABEL_PAD_V
        xs = range(int(point.x() + pad_h), int(point.x() + pad_h + metrics.horizontalAdvance("X")))
        ys = range(int(point.y() + pad_v), int(point.y() + pad_v + metrics.height()))
        painted = [result.pixelColor(x, y) for x in xs for y in ys]

        assert any(p == RED for p in painted)

    def test_chip_background_matches_tokens_colour_and_alpha(self):
        base = make_image(fill_color=BACKGROUND)
        point = QPointF(10, 10)
        text = Text(colour=RED, stroke_width=4, point=point, text="Hello")

        result = render(base, [text])

        # A couple of pixels in from the top-left corner: inside the
        # rounded corner's own arc (TEXT_LABEL_RADIUS=5) but well short of
        # the padding (TEXT_LABEL_PAD_H/_V) that keeps the glyph itself
        # away from this pixel -- chip fill only, no ring, no text.
        probe = result.pixelColor(int(point.x()) + 3, int(point.y()) + 3)
        assert probe != QColor(BACKGROUND)

        background = QColor(Color.TEXT_LABEL_BG)
        alpha = Color.TEXT_LABEL_BG_ALPHA
        expected_green = round(background.green() * alpha + 255 * (1 - alpha))
        assert probe.green() == pytest.approx(expected_green, abs=5)

    def test_chip_corners_are_rounded(self):
        base = make_image()
        point = QPointF(10, 10)
        text = Text(colour=RED, stroke_width=4, point=point, text="Hello")
        metrics = QFontMetricsF(text._font())
        chip_width = metrics.horizontalAdvance("Hello") + Metric.TEXT_LABEL_PAD_H * 2

        result = render(base, [text])

        # The exact top-left corner sits outside TEXT_LABEL_RADIUS's arc and
        # stays untouched background; a point on the flat top edge, well
        # clear of either corner, is fully covered chip fill.
        corner = result.pixelColor(int(point.x()), int(point.y()))
        edge = result.pixelColor(int(point.x() + chip_width / 2), int(point.y()))

        assert corner == QColor(BACKGROUND)
        assert edge != QColor(BACKGROUND)


class TestStepMarkerRendering:
    def test_renders_a_filled_badge(self):
        base = make_image()
        marker = StepMarker(colour=RED, stroke_width=4, point=QPointF(50, 50))

        result = render(base, [marker])

        assert result.pixelColor(50, 50) == RED

    def test_diameter_is_fixed_regardless_of_stroke_width(self):
        # STEP_D is a constant in the design, unlike every drawing tool --
        # a thin and a thick stroke must produce the exact same badge size.
        thin = StepMarker(colour=RED, stroke_width=1, point=QPointF(50, 50))
        thick = StepMarker(colour=RED, stroke_width=20, point=QPointF(50, 50))

        thin_result = render(make_image(), [thin])
        thick_result = render(make_image(), [thick])

        radius = Metric.STEP_D / 2
        inside = (50 + round(radius) - 2, 50)
        # Clear of the shadow's own spread too (see
        # test_has_a_soft_drop_shadow_below_the_badge), not just the fill.
        outside = (50 + round(radius) + 8, 50)

        assert thin_result.pixelColor(*inside) != QColor(BACKGROUND)
        assert thick_result.pixelColor(*inside) != QColor(BACKGROUND)
        assert thin_result.pixelColor(*outside) == QColor(BACKGROUND)
        assert thick_result.pixelColor(*outside) == QColor(BACKGROUND)

    def test_has_a_ring_distinct_from_the_fill(self):
        base = make_image()
        marker = StepMarker(colour=RED, stroke_width=4, point=QPointF(50, 50))

        result = render(base, [marker])

        radius = Metric.STEP_D / 2
        # Right at the circle's own edge (the ring straddles it) versus
        # solidly inside the fill -- the ring's white must show through
        # distinctly from the plain ink-coloured interior.
        ring_pixel = result.pixelColor(50 + round(radius) - 1, 50)
        fill_pixel = result.pixelColor(50, 50)

        assert ring_pixel != fill_pixel

    def test_has_a_soft_drop_shadow_below_the_badge(self):
        base = make_image()
        marker = StepMarker(colour=RED, stroke_width=4, point=QPointF(50, 50))

        result = render(base, [marker])

        radius = Metric.STEP_D / 2
        # Below the badge, outside the ring, is within the shadow's own
        # offset+spread reach -- must be darkened relative to untouched
        # background. The mirrored point above stays untouched, proving the
        # shadow is offset downward rather than a uniform halo around it.
        below = result.pixelColor(50, 50 + round(radius) + 2)
        above = result.pixelColor(50, 50 - round(radius) - 8)

        assert below != QColor(BACKGROUND)
        assert above == QColor(BACKGROUND)

    def test_badge_text_colour_and_font_come_from_tokens(self):
        assert StepMarker.BADGE_TEXT_COLOUR == QColor(Color.ACCENT_FG)

        marker = StepMarker(colour=RED, stroke_width=1, point=QPointF(0, 0))
        font = marker._font()
        size, weight = Font.STEP_BADGE

        assert font.pixelSize() == round(size)
        assert font.weight() == weight

    def test_font_size_is_fixed_regardless_of_stroke_width(self):
        # Unlike Text, the badge numeral doesn't derive its size from
        # stroke_width at all -- it's a fixed tokens.Font.STEP_BADGE size.
        thin = StepMarker(colour=RED, stroke_width=1, point=QPointF(0, 0))
        thick = StepMarker(colour=RED, stroke_width=20, point=QPointF(0, 0))

        assert thin._font().pixelSize() == thick._font().pixelSize()


class TestStepMarkerNumbering:
    def test_next_step_number_starts_at_one(self):
        assert next_step_number([]) == 1

    def test_next_step_number_counts_only_step_markers(self):
        shapes = [
            StepMarker(colour=RED, stroke_width=4, point=QPointF(10, 10), number=1),
            Rectangle(colour=RED, stroke_width=4, start=QPointF(0, 0), end=QPointF(5, 5)),
            StepMarker(colour=RED, stroke_width=4, point=QPointF(30, 30), number=2),
        ]

        assert next_step_number(shapes) == 3

    def test_render_does_not_mutate_stored_numbers(self):
        base = make_image()
        markers = [
            StepMarker(colour=RED, stroke_width=4, point=QPointF(10, 10), number=1),
            StepMarker(colour=RED, stroke_width=4, point=QPointF(30, 30), number=2),
            StepMarker(colour=RED, stroke_width=4, point=QPointF(50, 50), number=3),
        ]

        render(base, markers)

        assert [marker.number for marker in markers] == [1, 2, 3]

    def test_does_not_renumber_after_an_earlier_marker_is_removed(self):
        # Per docs/design/overlay-redesign.md's "Drawing": numbering is
        # count(existing steps) + 1 at creation, and deliberately does not
        # renumber after a delete -- matches the prototype.
        base = make_image()
        markers = [
            StepMarker(colour=RED, stroke_width=4, point=QPointF(10, 10), number=1),
            StepMarker(colour=RED, stroke_width=4, point=QPointF(30, 30), number=2),
            StepMarker(colour=RED, stroke_width=4, point=QPointF(50, 50), number=3),
        ]

        survivors = markers[1:]  # drop the first, as if the user removed it
        render(base, survivors)

        assert [marker.number for marker in survivors] == [2, 3]


class TestObscuringShapeFields:
    def test_blur_stores_colour_and_stroke_width(self):
        shape = Blur(colour=RED, stroke_width=3, start=QPointF(0, 0), end=QPointF(10, 10))
        assert shape.colour == RED
        assert shape.stroke_width == 3

    def test_pixelate_stores_colour_and_stroke_width(self):
        shape = Pixelate(colour=RED, stroke_width=3, start=QPointF(0, 0), end=QPointF(10, 10))
        assert shape.colour == RED
        assert shape.stroke_width == 3

    def test_blur_defaults_strength_to_the_token_default(self):
        shape = Blur(colour=RED, stroke_width=3, start=QPointF(0, 0), end=QPointF(10, 10))
        assert shape.strength == Metric.BLUR_DEFAULT

    def test_pixelate_defaults_strength_to_the_token_default(self):
        shape = Pixelate(colour=RED, stroke_width=3, start=QPointF(0, 0), end=QPointF(10, 10))
        assert shape.strength == Metric.BLUR_DEFAULT

    def test_strength_takes_an_explicit_value_within_the_token_range(self):
        shape = Pixelate(
            colour=RED,
            stroke_width=3,
            start=QPointF(0, 0),
            end=QPointF(10, 10),
            strength=Metric.BLUR_MAX,
        )
        assert shape.strength == Metric.BLUR_MAX

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
        # two lists — one with a solid stroke drawn first, one without — in
        # the same render() call each time, and showing the results differ
        # at a pixel inside the blur. A thick Pen line, not a Rectangle: a
        # rounded-rect stroke this wide relative to the shape no longer
        # reliably fills its own interior (SNX-35), where a plain polyline
        # stroke still does.
        stroke = Pen(
            colour=RED,
            stroke_width=300,  # wildly thick: covers the whole image
            points=[QPointF(0, 50), QPointF(100, 50)],
        )
        blur = Blur(colour=RED, stroke_width=4, start=QPointF(20, 20), end=QPointF(80, 80))

        with_stroke = render(make_image(), [stroke, blur])
        blur_alone = render(make_image(), [blur])

        probe = (50, 50)
        assert with_stroke.pixelColor(*probe) != blur_alone.pixelColor(*probe)

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

    def test_rect_extending_past_the_frame_is_clamped_not_raised(self):
        # A drag that starts inside the frame and is released past its
        # edge (or a shape translated there, see render_selection) must be
        # clamped to the frame rather than blowing up on QImage.copy()'s
        # undefined padding for an out-of-bounds rect.
        base = make_gradient_image(size=(40, 40))
        blur = Blur(colour=RED, stroke_width=4, start=QPointF(20, 20), end=QPointF(200, 200))

        result = render(base, [blur])  # must not raise

        assert result != base
        # The far corner of the clamped rect, at the frame's own edge, was
        # obscured...
        assert result.pixelColor(39, 39) != base.pixelColor(39, 39)
        # ...and the shape's clamp did nothing to pixels outside its rect.
        assert result.pixelColor(5, 5) == base.pixelColor(5, 5)


class TestPixelateBlocky:
    def test_uniform_within_a_block_varies_across_blocks(self):
        size = 80
        strength = Metric.BLUR_DEFAULT
        base = make_gradient_image(size=(size, size))
        pixelate = Pixelate(
            colour=RED, stroke_width=4, start=QPointF(0, 0), end=QPointF(size, size),
            strength=strength,
        )

        result = render(base, [pixelate])

        # Block width is derived from the shape's own strength rather than
        # hardcoded, so retuning BLUR_DEFAULT doesn't break this test's
        # premise.
        block_width = size // (size // strength)
        # Two pixels a couple of px apart inside block 0 must match...
        assert result.pixelColor(1, 40) == result.pixelColor(3, 40)
        # ...but a pixel a full block away is not required to, and for this
        # gradient does not.
        assert result.pixelColor(1, 40) != result.pixelColor(block_width + 1, 40)

    def test_higher_strength_downsamples_to_wider_blocks(self):
        # "downsamples by the strength" (docs/design/overlay-redesign.md's
        # "blur" entry): a higher strength means a smaller intermediate
        # image and so coarser, wider blocks once scaled back up.
        size = 80
        base = make_gradient_image(size=(size, size))
        low = Pixelate(
            colour=RED, stroke_width=4, start=QPointF(0, 0), end=QPointF(size, size),
            strength=Metric.BLUR_MIN,
        )
        high = Pixelate(
            colour=RED, stroke_width=4, start=QPointF(0, 0), end=QPointF(size, size),
            strength=Metric.BLUR_MAX,
        )

        low_result = render(QImage(base), [low])
        high_result = render(QImage(base), [high])

        low_block_width = size // (size // Metric.BLUR_MIN)
        high_block_width = size // (size // Metric.BLUR_MAX)
        assert high_block_width > low_block_width
        # A pixel just past the low-strength block boundary already
        # changed colour for `low`, but the coarser `high` block still
        # spans it, so its two neighbouring blocks read as still equal.
        probe_a, probe_b = 1, low_block_width + 1
        assert low_result.pixelColor(probe_a, 40) != low_result.pixelColor(probe_b, 40)
        assert high_result.pixelColor(probe_a, 40) == high_result.pixelColor(probe_b, 40)

    def test_pixelate_distinguishable_from_blur_over_same_region(self):
        blur_base = make_gradient_image(size=(80, 80))
        pixelate_base = make_gradient_image(size=(80, 80))
        rect = (QPointF(0, 0), QPointF(80, 80))

        blur_result = render(blur_base, [Blur(colour=RED, stroke_width=4, start=rect[0], end=rect[1])])
        pixelate_result = render(
            pixelate_base, [Pixelate(colour=RED, stroke_width=4, start=rect[0], end=rect[1])]
        )

        assert blur_result != pixelate_result


class TestRectangleGeometry:
    def test_is_unfilled(self):
        base = make_image()
        rect = Rectangle(
            colour=RED, stroke_width=4, start=QPointF(10, 10), end=QPointF(70, 70)
        )

        result = render(base, [rect])

        assert result.pixelColor(40, 40) == QColor(BACKGROUND)  # interior: untouched

    def test_corners_are_rounded(self):
        base = make_image()
        # A thin stroke, so the rounded arc's own width doesn't eat the
        # margin this assertion depends on.
        rect = Rectangle(
            colour=RED, stroke_width=1, start=QPointF(10, 10), end=QPointF(50, 50)
        )

        result = render(base, [rect])

        # The exact top-left corner sits farther from the rounded arc's
        # centre than the straight edge does from its own path, so a sharp
        # corner would be at least as covered as the edge -- the rounded
        # one leaves it markedly *less* covered instead. Compared via the
        # green channel (RED blended with a white background: 255 where
        # nothing is painted, lower the more opaque red coverage a pixel
        # got) rather than exact equality, since antialiasing at a 1px
        # stroke leaves both pixels partially covered, not binary.
        corner_coverage = result.pixelColor(10, 10).green()
        edge_coverage = result.pixelColor(10, 30).green()
        assert corner_coverage > edge_coverage + 50


class TestFinalizeMark:
    def test_freehand_with_one_point_is_discarded(self):
        pen = Pen(colour=RED, stroke_width=4, points=[QPointF(10, 10)])
        highlighter = Highlighter(colour=RED, stroke_width=4, points=[QPointF(10, 10)])

        assert finalize_mark(pen) is None
        assert finalize_mark(highlighter) is None

    def test_freehand_with_multiple_points_survives_unchanged(self):
        pen = Pen(colour=RED, stroke_width=4, points=[QPointF(10, 10), QPointF(20, 20)])

        assert finalize_mark(pen) is pen

    def test_shape_smaller_than_the_drop_threshold_in_both_axes_is_discarded(self):
        tiny = Rectangle(
            colour=RED, stroke_width=4, start=QPointF(10, 10),
            end=QPointF(10 + DROP_THRESHOLD, 10 + DROP_THRESHOLD),
        )

        assert finalize_mark(tiny) is None

    def test_shape_past_the_threshold_in_only_one_axis_survives(self):
        # A deliberate horizontal drag: zero height, well past the
        # threshold in width -- must not be treated as a stray click.
        horizontal = Rectangle(
            colour=RED, stroke_width=4, start=QPointF(10, 10), end=QPointF(50, 10)
        )
        vertical_arrow = Arrow(
            colour=RED, stroke_width=4, start=QPointF(10, 10), end=QPointF(10, 50)
        )

        assert finalize_mark(horizontal) is not None
        assert finalize_mark(vertical_arrow) is vertical_arrow

    def test_rectangle_dragged_up_left_is_normalised_on_release(self):
        dragged_up_left = Rectangle(
            colour=RED, stroke_width=4, start=QPointF(50, 50), end=QPointF(10, 10)
        )

        result = finalize_mark(dragged_up_left)

        assert result.start == QPointF(10, 10)
        assert result.end == QPointF(50, 50)

    def test_arrow_direction_is_preserved_not_normalised(self):
        # Unlike Rectangle, an Arrow dragged "backwards" (tail bottom-right,
        # head top-left) must keep start=tail/end=head -- normalising its
        # corners the way Rectangle's are would silently flip the arrow.
        arrow = Arrow(colour=RED, stroke_width=4, start=QPointF(50, 50), end=QPointF(10, 10))

        result = finalize_mark(arrow)

        assert result.start == QPointF(50, 50)
        assert result.end == QPointF(10, 10)

    def test_other_shapes_commit_unchanged(self):
        text = Text(colour=RED, stroke_width=4, point=QPointF(5, 5), text="hi")

        assert finalize_mark(text) is text


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
        # A stroke straddling the crop boundary, thick enough to paint
        # solidly at (25, 25) crop-relative == (35, 35) image-absolute.
        mark = Pen(
            colour=RED, stroke_width=100, points=[QPointF(20, 20), QPointF(50, 50)]
        )
        crop_rect = QRectF(10, 10, 40, 40)

        result = apply_crop(frame, [mark], crop_rect)

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


class TestRenderSelection:
    """SNX-34: the export path for OverlayWindow's ink layer. Unlike
    apply_crop (used by the old editor.py Canvas), the shapes passed in here
    are in overlay-window coordinates -- local to `frame`'s own top-left,
    the same space `selection` is in -- not already matching the base
    image's own pixel space.
    """

    def _frame(self, image_size=(200, 200), logical_size=None, logical_origin=(0, 0)):
        image = make_image(size=image_size)
        logical_size = logical_size or QSizeF(*image_size)
        return Frame(
            image=image,
            logical_origin=QPointF(*logical_origin),
            logical_size=logical_size,
        )

    def test_positions_a_mark_at_its_window_coordinates_inside_the_selection(self):
        frame = self._frame(image_size=(300, 300))
        # A mark drawn well inside a selection whose own top-left is (50, 50).
        mark = Rectangle(
            colour=RED, stroke_width=6, start=QPointF(60, 60), end=QPointF(120, 120)
        )
        selection = QRectF(50, 50, 100, 100)

        result = render_selection(frame, [mark], selection)

        assert result.width() == 100
        assert result.height() == 100
        # (60, 60) in window coordinates is (10, 10) once selection's own
        # origin is translated away -- this is the ticket's one translation.
        assert result.pixelColor(10, 40) == RED  # left border

    def test_mark_outside_the_selection_is_not_painted(self):
        frame = self._frame(image_size=(300, 300))
        mark = Rectangle(
            colour=RED, stroke_width=6, start=QPointF(10, 10), end=QPointF(30, 30)
        )
        selection = QRectF(50, 50, 100, 100)

        result = render_selection(frame, [mark], selection)

        # A mark that never falls inside the exported crop leaves it exactly
        # as an un-annotated crop would look -- present, never deleted, just
        # not painted this time (mirrors OverlayWindow's live clip).
        assert result == frame.crop(selection).image

    def test_selection_origin_is_translated_only_once(self):
        # A window whose own origin is away from (0, 0) (a monitor left of
        # the virtual desktop's primary) must not double-apply that offset:
        # a mark's position within the exported crop depends only on where
        # it sits relative to `selection`, not on `frame.logical_origin`.
        frame = self._frame(image_size=(300, 300), logical_origin=(500, 300))
        mark = Rectangle(
            colour=RED, stroke_width=6, start=QPointF(60, 60), end=QPointF(120, 120)
        )
        selection = QRectF(50, 50, 100, 100)

        result = render_selection(frame, [mark], selection)

        assert result.pixelColor(10, 40) == RED

    def test_scales_marks_into_image_pixel_space_under_display_scaling(self):
        # image is 2x logical: a mark's window-coordinate point must land at
        # twice its logical offset in the exported pixels, the same ratio
        # Frame.crop() itself derives.
        frame = self._frame(image_size=(200, 200), logical_size=QSizeF(100, 100))
        mark = Rectangle(
            colour=RED, stroke_width=4, start=QPointF(10, 10), end=QPointF(40, 40)
        )
        selection = QRectF(0, 0, 100, 100)

        result = render_selection(frame, [mark], selection)

        assert result.width() == 200
        assert result.height() == 200
        assert result.pixelColor(20, 50) == RED  # left border at 2x scale

    def test_bakes_marks_in_draw_order_like_render(self):
        frame = self._frame(image_size=(100, 100))
        first = Rectangle(
            colour=RED, stroke_width=6, start=QPointF(10, 10), end=QPointF(70, 70)
        )
        second = Rectangle(
            colour=BLUE, stroke_width=6, start=QPointF(10, 10), end=QPointF(70, 70)
        )
        selection = QRectF(0, 0, 100, 100)

        result = render_selection(frame, [first, second], selection)

        assert result.pixelColor(10, 40) == BLUE
