import itertools
from dataclasses import dataclass, field, replace

import pytest
from PyQt6.QtCore import Qt, QPointF, QRect, QRectF, QSizeF
from PyQt6.QtGui import (
    QColor,
    QFontMetrics,
    QFontMetricsF,
    QImage,
    QPainter,
    qBlue,
    qGreen,
    qRed,
    qRgb,
)
from PyQt6.QtWidgets import QApplication

from snipux.capture import Frame
from snipux.design.tokens import (
    DASH_CYCLE,
    FILL_OPACITY,
    WATERMARK,
    Color,
    Font,
    Metric,
    WatermarkMetric,
)
from snipux.shapes import (
    Arrow,
    Blur,
    Callout,
    Crop,
    DROP_THRESHOLD,
    Ellipse,
    Highlighter,
    Line,
    Pen,
    Pixelate,
    Rectangle,
    Redact,
    Shape,
    Spotlight,
    StepMarker,
    Text,
    Watermark,
    apply_crop,
    finalize_mark,
    next_step_number,
    render,
    render_selection,
    _line_pen,
    _transformed,
)

BACKGROUND = qRgb(255, 255, 255)
RED = QColor(255, 0, 0)
BLUE = QColor(0, 0, 255)
# No channel at 0 or 255, so a tint of it can only be this colour's own.
TEAL = QColor(20, 140, 160)
DASH_PATTERNS = {name: pattern for name, pattern, _label in DASH_CYCLE}


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


def ink_runs(image: QImage, y: int, x_from: int, x_to: int) -> list[tuple[bool, int]]:
    """Each unbroken run of pixels along row `y`, as (inked, length) -- for a
    dashed line, its dashes and gaps in order. Inked means the green channel
    is nearer ink's 0 than the white background's 255, which holds for RED
    and BLUE alike, so an antialiased edge pixel lands on one side or the
    other rather than being a third thing.
    """
    runs: list[list] = []
    for x in range(x_from, x_to):
        inked = image.pixelColor(x, y).green() < 128
        if runs and runs[-1][0] == inked:
            runs[-1][1] += 1
        else:
            runs.append([inked, 1])
    return [(inked, length) for inked, length in runs]


def ink_edges(image: QImage, y: int, x_from: int, x_to: int) -> list[int]:
    """The x positions along row `y` where ink starts or stops."""
    lengths = (length for _inked, length in ink_runs(image, y, x_from, x_to))
    return [x_from + edge for edge in itertools.accumulate(lengths)][:-1]


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

        # Sampled 7px off centre, not at it: the badge's own number is drawn
        # across the middle, so the centre pixel is whatever colour that
        # glyph happens to be in the font this machine has. CI paints a
        # digit wide enough to cover the centre; this machine does not.
        # 7px is inside the fill (radius 13, less the 2px ring) and clear of
        # any digit.
        assert result.pixelColor(57, 50) == BLUE  # badge fill
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

        # Off centre: see the note in TestRender above -- the number sits in
        # the middle of the badge and is font-dependent.
        assert result.pixelColor(57, 50) == RED
        assert result.pixelColor(50, 50) != QColor(BACKGROUND)  # badge covers it

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


class TestRedact:
    """A solid fill leaves nothing of the original behind -- the property
    Blur and Pixelate lack, and the reason automatic hiding uses this one.
    """

    def test_every_pixel_inside_is_black(self):
        base = make_gradient_image(size=(80, 80))

        result = render(base, [Redact(colour=RED, stroke_width=4, start=QPointF(10, 10), end=QPointF(50, 40))])

        for x in (10, 30, 49):
            for y in (10, 25, 39):
                assert result.pixelColor(x, y) == QColor(0, 0, 0)

    def test_pixels_outside_are_untouched(self):
        base = make_gradient_image(size=(80, 80))

        result = render(base, [Redact(colour=RED, stroke_width=4, start=QPointF(10, 10), end=QPointF(50, 40))])

        assert result.pixelColor(70, 70) == base.pixelColor(70, 70)
        assert result.pixelColor(5, 5) == base.pixelColor(5, 5)

    def test_output_does_not_depend_on_what_was_underneath(self):
        # The recoverability argument, as an assertion: two different
        # images under the same box must export identically inside it.
        a = make_gradient_image(size=(80, 80))
        b = make_image(size=(80, 80), fill_color=qRgb(12, 200, 90))
        box = dict(colour=RED, stroke_width=4, start=QPointF(0, 0), end=QPointF(80, 80))

        assert render(a, [Redact(**box)]) == render(b, [Redact(**box)])

    def test_either_corner_order_fills_the_same_rect(self):
        base = make_gradient_image(size=(80, 80))
        forward = Redact(colour=RED, stroke_width=4, start=QPointF(10, 10), end=QPointF(50, 40))
        backward = Redact(colour=RED, stroke_width=4, start=QPointF(50, 40), end=QPointF(10, 10))

        assert render(base, [forward]) == render(base, [backward])

    def test_does_not_mutate_the_base_image(self):
        base = make_gradient_image(size=(80, 80))
        before = QImage(base)

        render(base, [Redact(colour=RED, stroke_width=4, start=QPointF(0, 0), end=QPointF(80, 80))])

        assert base == before


class TestSpotlight:
    """The inverse of Redact: dims everything *outside* its rect, and
    leaves the inside alone.
    """

    def test_pixels_inside_are_untouched(self):
        base = make_gradient_image(size=(80, 80))
        spot = Spotlight(colour=RED, stroke_width=4, start=QPointF(10, 10), end=QPointF(50, 40))

        result = render(base, [spot])

        for x in (10, 30, 49):
            for y in (10, 25, 39):
                assert result.pixelColor(x, y) == base.pixelColor(x, y)

    def test_pixels_outside_are_dimmed(self):
        base = make_image(size=(80, 80))
        spot = Spotlight(colour=RED, stroke_width=4, start=QPointF(10, 10), end=QPointF(50, 40))

        result = render(base, [spot])
        outside, before = result.pixelColor(70, 70), base.pixelColor(70, 70)

        assert outside != before
        # A translucent near-black laid over white: every channel drops.
        assert outside.red() < before.red()
        assert outside.green() < before.green()
        assert outside.blue() < before.blue()

    def test_two_spotlights_both_stay_lit(self):
        # The property `apply_all` exists for: each rect stays lit even
        # though neither spotlight knows about the other.
        base = make_image(size=(100, 100))
        first = Spotlight(colour=RED, stroke_width=4, start=QPointF(5, 5), end=QPointF(25, 25))
        second = Spotlight(colour=RED, stroke_width=4, start=QPointF(60, 60), end=QPointF(90, 90))

        result = render(base, [first, second])

        assert result.pixelColor(15, 15) == base.pixelColor(15, 15)
        assert result.pixelColor(75, 75) == base.pixelColor(75, 75)
        # Between the two: dimmed, same as anywhere outside a single one.
        assert result.pixelColor(40, 40) != base.pixelColor(40, 40)

    def test_overlapping_spotlights_stay_lit_where_they_overlap(self):
        # A plain even-odd path (`_paint_scrim`'s own trick) breaks here: a
        # point inside both rects crosses the outer boundary and both
        # holes -- three subpaths, an odd count -- and comes out filled
        # again. `apply_all` builds the holes as a `QRegion` union first,
        # so an overlap only ever merges.
        base = make_image(size=(100, 100))
        first = Spotlight(colour=RED, stroke_width=4, start=QPointF(10, 10), end=QPointF(50, 50))
        second = Spotlight(colour=RED, stroke_width=4, start=QPointF(30, 30), end=QPointF(70, 70))

        result = render(base, [first, second])

        assert result.pixelColor(40, 40) == base.pixelColor(40, 40)  # the overlap
        assert result.pixelColor(15, 15) == base.pixelColor(15, 15)  # first only
        assert result.pixelColor(65, 65) == base.pixelColor(65, 65)  # second only
        assert result.pixelColor(90, 90) != base.pixelColor(90, 90)  # outside both

    def test_a_second_spotlight_does_not_redim_the_firsts_hole(self):
        # Were each spotlight applied on its own turn rather than combined,
        # the second one's "dim everything outside *my* rect" would darken
        # the first one's hole again the moment it ran -- only the rects'
        # intersection would stay lit instead of their union. Proven by
        # comparing against the first spotlight rendered alone: the pixel
        # inside only the first rect must come out identically either way.
        base = make_image(size=(100, 100))
        first = Spotlight(colour=RED, stroke_width=4, start=QPointF(5, 5), end=QPointF(25, 25))
        second = Spotlight(colour=RED, stroke_width=4, start=QPointF(60, 60), end=QPointF(90, 90))

        together = render(base, [first, second])
        alone = render(QImage(base), [first])

        assert together.pixelColor(15, 15) == alone.pixelColor(15, 15)

    def test_higher_strength_dims_more(self):
        base = make_image(size=(80, 80))
        rect = dict(start=QPointF(10, 10), end=QPointF(50, 40))
        light = Spotlight(colour=RED, stroke_width=4, strength=Metric.BLUR_MIN, **rect)
        heavy = Spotlight(colour=RED, stroke_width=4, strength=Metric.BLUR_MAX, **rect)

        light_result = render(QImage(base), [light])
        heavy_result = render(QImage(base), [heavy])

        assert heavy_result.pixelColor(70, 70).red() < light_result.pixelColor(70, 70).red()

    def test_default_strength_reproduces_the_selection_scrims_own_dim(self):
        # "Something that still reads rather than black" is already this
        # app's own answer for dimming outside a lit region -- the
        # selection scrim's DIM_ALPHA -- not a new number invented here.
        spot = Spotlight(colour=RED, stroke_width=4, start=QPointF(0, 0), end=QPointF(10, 10))

        assert spot.strength == Metric.BLUR_DEFAULT
        assert spot._alpha() == pytest.approx(Color.DIM_ALPHA)

    def test_degenerate_rect_is_a_no_op(self):
        base = make_image()
        spot = Spotlight(colour=RED, stroke_width=4, start=QPointF(30, 30), end=QPointF(30, 30))

        result = render(base, [spot])

        assert result == base

    def test_does_not_mutate_the_base_image(self):
        base = make_gradient_image(size=(80, 80))
        before = QImage(base)

        render(base, [Spotlight(colour=RED, stroke_width=4, start=QPointF(0, 0), end=QPointF(40, 40))])

        assert base == before

    def test_undo_removes_it(self):
        # `MarkStore` is geometry-agnostic, so undoing an add is already
        # covered generically in test_marks.py -- this is the acceptance
        # criterion itself: undo a spotlight and its dim is gone from what
        # actually renders, the same as removing it from the shape list by
        # hand.
        from snipux.marks import MarkStore

        base = make_image(size=(80, 80))
        store = MarkStore()
        store.add(Spotlight(colour=RED, stroke_width=4, start=QPointF(10, 10), end=QPointF(50, 40)))

        store.undo()

        assert len(store) == 0
        assert render(base, list(store.marks)) == base


class TestRectangleGeometry:
    # #65 gave rectangles a fill. `outline` is the default, so a rectangle
    # made the way every caller made one before is still unfilled -- and so
    # is one that asks for `outline` by name.
    @pytest.mark.parametrize("style", [{}, {"fill": "outline"}], ids=["default", "outline"])
    def test_is_unfilled(self, style):
        base = make_image()
        rect = Rectangle(
            colour=RED, stroke_width=4, start=QPointF(10, 10), end=QPointF(70, 70), **style
        )

        result = render(base, [rect])

        assert result.pixelColor(40, 40) == QColor(BACKGROUND)  # interior: untouched
        assert result.pixelColor(10, 40) == RED  # the outline is still there

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


class TestCalloutGeometry:
    """`start` is the tail's own tip -- where the drag began -- and the
    body is the drag's bounding rect, pulled in by `TAIL_LENGTH` at
    whichever corner `start` sits on. See `Callout._geometry`.
    """

    def test_body_is_the_drag_inset_by_the_tail_notch(self):
        callout = Callout(
            colour=RED, stroke_width=4, start=QPointF(10, 10), end=QPointF(110, 90)
        )

        body = callout.body_rect()

        assert body == QRectF(QPointF(10 + Callout.TAIL_LENGTH, 10 + Callout.TAIL_LENGTH),
                               QPointF(110, 90))

    def test_tail_tip_is_where_the_drag_began_not_where_it_ended(self):
        callout = Callout(
            colour=RED, stroke_width=4, start=QPointF(10, 10), end=QPointF(110, 90)
        )

        _body, tail = callout._geometry()

        assert tail[0] == QPointF(10, 10)

    def test_body_follows_whichever_corner_start_dragged_from(self):
        # Dragged from the bottom-right this time: the notch -- and the
        # tail -- move to that corner instead.
        callout = Callout(
            colour=RED, stroke_width=4, start=QPointF(110, 90), end=QPointF(10, 10)
        )

        body = callout.body_rect()

        assert body == QRectF(QPointF(10, 10),
                               QPointF(110 - Callout.TAIL_LENGTH, 90 - Callout.TAIL_LENGTH))

    def test_a_zero_width_drag_leaves_no_room_for_a_tail(self):
        # Below DROP_THRESHOLD this never reaches finalize_mark, but
        # _geometry itself must not produce a negative notch, or divide by
        # zero, for a drag with no width to inset a corner from.
        callout = Callout(
            colour=RED, stroke_width=4, start=QPointF(10, 10), end=QPointF(10, 90)
        )

        body, tail = callout._geometry()

        assert body.width() == 0
        assert tail is None


class TestCalloutRendering:
    def test_body_outline_is_painted_and_its_interior_is_not(self):
        base = make_image(size=(200, 200))
        callout = Callout(
            colour=RED, stroke_width=4, start=QPointF(10, 10), end=QPointF(110, 90)
        )

        result = render(base, [callout])

        body = callout.body_rect()
        assert result.pixelColor(round(body.left()), round(body.center().y())) == RED
        assert result.pixelColor(
            round(body.center().x()), round(body.center().y())
        ) == QColor(BACKGROUND)

    def test_the_tail_is_painted_from_its_tip_to_the_body(self):
        base = make_image(size=(200, 200))
        callout = Callout(
            colour=RED, stroke_width=4, start=QPointF(10, 10), end=QPointF(110, 90)
        )

        result = render(base, [callout])

        _body, tail = callout._geometry()
        tip, base_a = tail[0], tail[1]
        midpoint = QPointF((tip.x() + base_a.x()) / 2, (tip.y() + base_a.y()) / 2)
        assert result.pixelColor(round(midpoint.x()), round(midpoint.y())).green() < 128

    def test_text_paints_inside_the_body_in_the_marks_colour(self):
        base = make_image(size=(200, 200))
        callout = Callout(
            colour=RED, stroke_width=6, start=QPointF(10, 10), end=QPointF(150, 100),
            text="Hi",
        )

        result = render(base, [callout])

        rect = callout._text_rect(callout.body_rect())
        metrics = QFontMetricsF(callout._font())
        xs = range(int(rect.left()), int(rect.left() + metrics.horizontalAdvance("Hi")) + 1)
        ys = range(int(rect.top()), int(rect.top() + metrics.height()) + 1)
        painted = [result.pixelColor(x, y) for x in xs for y in ys]

        assert any(p == RED for p in painted)

    def test_empty_text_paints_no_glyphs(self):
        base = make_image(size=(200, 200))
        with_text = render(base, [Callout(
            colour=RED, stroke_width=4, start=QPointF(10, 10), end=QPointF(150, 100), text="X",
        )])
        without_text = render(base, [Callout(
            colour=RED, stroke_width=4, start=QPointF(10, 10), end=QPointF(150, 100), text="",
        )])

        assert with_text != without_text


class TestCalloutWrapping:
    """The wrap point depends on the font this mark actually draws with --
    worked out from `QFontMetrics` below rather than assumed, per
    CLAUDE.md: CI's Ubuntu runner only has DejaVu Sans, wider than this
    desk's own font, and Windows rasterises text differently again.
    """

    def test_empty_text_wraps_to_no_lines(self):
        callout = Callout(
            colour=RED, stroke_width=4, start=QPointF(10, 10), end=QPointF(90, 70)
        )

        assert callout.wrapped_lines() == []

    def test_text_narrower_than_the_body_stays_on_one_line(self):
        callout = Callout(
            colour=RED, stroke_width=4, start=QPointF(10, 10), end=QPointF(300, 200), text="Hi",
        )

        assert callout.wrapped_lines() == ["Hi"]

    def test_long_text_wraps_without_losing_or_reordering_any_word(self):
        callout = Callout(
            colour=RED, stroke_width=4, start=QPointF(10, 10), end=QPointF(90, 70),
            text="one two three four five six seven eight nine ten",
        )
        metrics = QFontMetricsF(callout._font())
        max_width = callout._text_rect(callout.body_rect()).width()

        lines = callout.wrapped_lines()

        assert len(lines) > 1
        assert " ".join(lines).split() == callout.text.split()
        for line in lines:
            assert metrics.horizontalAdvance(line) <= max_width + 0.5

    def test_a_single_word_wider_than_the_body_gets_its_own_line(self):
        callout = Callout(
            colour=RED, stroke_width=4, start=QPointF(10, 10), end=QPointF(50, 70),
            text="a supercalifragilisticexpialidocious word",
        )

        lines = callout.wrapped_lines()

        assert "supercalifragilisticexpialidocious" in lines


class TestCalloutHitTest:
    def test_hits_the_body_outline_not_its_interior(self):
        callout = Callout(
            colour=RED, stroke_width=4, start=QPointF(10, 10), end=QPointF(110, 90)
        )
        body = callout.body_rect()

        assert callout.hit_test(QPointF(body.left(), body.center().y())) is True
        assert callout.hit_test(body.center()) is False

    def test_hits_the_tail(self):
        callout = Callout(
            colour=RED, stroke_width=4, start=QPointF(10, 10), end=QPointF(110, 90)
        )
        _body, tail = callout._geometry()
        tip, base_a = tail[0], tail[1]
        midpoint = QPointF((tip.x() + base_a.x()) / 2, (tip.y() + base_a.y()) / 2)

        assert callout.hit_test(midpoint) is True

    def test_misses_well_clear_of_the_mark(self):
        callout = Callout(
            colour=RED, stroke_width=4, start=QPointF(10, 10), end=QPointF(110, 90)
        )

        assert callout.hit_test(QPointF(500, 500)) is False

    def test_filled_callout_counts_its_interior_as_a_hit(self):
        callout = Callout(
            colour=RED, stroke_width=4, start=QPointF(10, 10), end=QPointF(110, 90),
            fill="filled",
        )
        body = callout.body_rect()

        assert callout.hit_test(body.center()) is True


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

    def test_normalising_a_rectangle_keeps_its_style(self):
        # Normalising builds a new Rectangle; the style must come with it.
        dragged_up_left = Rectangle(
            colour=RED, stroke_width=4, start=QPointF(50, 50), end=QPointF(10, 10),
            fill="both", dash="dotted",
        )

        result = finalize_mark(dragged_up_left)

        assert (result.fill, result.dash) == ("both", "dotted")

    def test_arrow_direction_is_preserved_not_normalised(self):
        # Unlike Rectangle, an Arrow dragged "backwards" (tail bottom-right,
        # head top-left) must keep start=tail/end=head -- normalising its
        # corners the way Rectangle's are would silently flip the arrow.
        arrow = Arrow(colour=RED, stroke_width=4, start=QPointF(50, 50), end=QPointF(10, 10))

        result = finalize_mark(arrow)

        assert result.start == QPointF(50, 50)
        assert result.end == QPointF(10, 10)

    @pytest.mark.parametrize("shape_class", [Ellipse, Line, Crop, Callout])
    def test_restored_shape_tools_discard_a_stray_click(self, shape_class):
        # SNX-64: Ellipse/Line/Crop joined Arrow/Rectangle/ObscuringShape in
        # finalize_mark's drop-threshold check when they were wired up as
        # overlay.py mark tools -- a click too small to be a deliberate
        # drag must not leave an invisible mark behind for any of them,
        # same as it already didn't for Rectangle. Callout joined the same
        # group when it was added.
        tiny = shape_class(
            colour=RED, stroke_width=4, start=QPointF(10, 10),
            end=QPointF(10 + DROP_THRESHOLD, 10 + DROP_THRESHOLD),
        )

        assert finalize_mark(tiny) is None

    @pytest.mark.parametrize("shape_class", [Ellipse, Line, Crop])
    def test_restored_shape_tools_keep_their_own_corner_order(self, shape_class):
        # Unlike Rectangle, none of these three depend on a particular
        # corner order (Ellipse/Crop's own draw()/hit_test() both already
        # normalise internally via _rect_from_corners; Line has no notion
        # of "corners" at all) -- so, like Arrow, they come back exactly as
        # dragged.
        dragged_up_left = shape_class(
            colour=RED, stroke_width=4, start=QPointF(50, 50), end=QPointF(10, 10)
        )

        result = finalize_mark(dragged_up_left)

        assert result.start == QPointF(50, 50)
        assert result.end == QPointF(10, 10)

    def test_callout_keeps_its_corner_order_since_start_is_the_tail_tip(self):
        # Unlike the shapes above, Callout's start isn't just "a corner" --
        # it's where the drag began, the tail's own tip. Normalising it
        # away the way Rectangle's is would silently move what the mark is
        # pointing at.
        dragged_up_left = Callout(
            colour=RED, stroke_width=4, start=QPointF(50, 50), end=QPointF(10, 10)
        )

        result = finalize_mark(dragged_up_left)

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


class TestShapeHitTest:
    """SNX-38: each shape's own answer to "does this point land on me,"
    which OverlayWindow's eraser (erase_at, overlay.py) uses to pick the
    topmost mark under a click. Shape is coordinate-space agnostic (see
    the module docstring), so these points aren't asserted to be any
    particular image/window space -- just whichever space a given test's
    shape happens to use.
    """

    def test_pen_with_fewer_than_two_points_is_never_hit(self):
        # No segment exists yet to hit-test against -- a bare click, not a
        # drag, never reaches a second mouseMoveEvent to append one.
        pen = Pen(colour=RED, stroke_width=4, points=[QPointF(10, 10)])

        assert pen.hit_test(QPointF(10, 10)) is False

    def test_pen_hits_along_its_polyline(self):
        pen = Pen(
            colour=RED, stroke_width=4,
            points=[QPointF(0, 0), QPointF(50, 0), QPointF(50, 50)],
        )

        assert pen.hit_test(QPointF(25, 0)) is True  # midpoint of the first segment
        assert pen.hit_test(QPointF(50, 25)) is True  # midpoint of the second segment
        assert pen.hit_test(QPointF(200, 200)) is False

    def test_highlighter_hit_test_uses_its_own_widened_stroke(self):
        # Highlighter widens its painted stroke to stroke_width x
        # HIGHLIGHT_MULT (its own _pen() override) -- a point that misses a
        # same-stroke-width Pen must still hit an identically-placed
        # Highlighter, since _stroke_hit_test reads the width from _pen()
        # rather than stroke_width directly.
        points = [QPointF(0, 0), QPointF(100, 0)]
        pen = Pen(colour=RED, stroke_width=2, points=list(points))
        highlighter = Highlighter(colour=RED, stroke_width=2, points=list(points))
        off_line_point = QPointF(50, 6)  # 6px above the shared line

        assert pen.hit_test(off_line_point) is False
        assert highlighter.hit_test(off_line_point) is True

    def test_thin_line_still_hits_within_the_tolerance(self):
        # AC: "a hit test on a stroked shape allows for the stroke width,
        # so a click on a thin line still hits it" -- a 1px-wide line's
        # actual painted width alone (0.5px either side) would never catch
        # an ordinary click; HIT_TOLERANCE is what makes it clickable.
        line = Line(colour=RED, stroke_width=1, start=QPointF(0, 20), end=QPointF(100, 20))

        assert line.hit_test(QPointF(50, 20)) is True  # dead centre
        assert line.hit_test(QPointF(50, 22)) is True  # 2px off, within tolerance
        assert line.hit_test(QPointF(50, 60)) is False  # well clear of the line

    def test_arrow_hits_along_its_shaft(self):
        arrow = Arrow(colour=RED, stroke_width=3, start=QPointF(0, 0), end=QPointF(100, 0))

        assert arrow.hit_test(QPointF(50, 0)) is True
        assert arrow.hit_test(QPointF(50, 60)) is False

    def test_rectangle_hits_its_border_not_its_interior(self):
        rect = Rectangle(colour=RED, stroke_width=4, start=QPointF(10, 10), end=QPointF(60, 60))

        assert rect.hit_test(QPointF(10, 35)) is True  # left border
        assert rect.hit_test(QPointF(35, 35)) is False  # empty interior: not a hit
        assert rect.hit_test(QPointF(200, 200)) is False

    def test_ellipse_hits_its_border_not_its_interior(self):
        ellipse = Ellipse(colour=RED, stroke_width=4, start=QPointF(0, 0), end=QPointF(100, 60))

        assert ellipse.hit_test(QPointF(50, 0)) is True  # top of the ellipse's border
        assert ellipse.hit_test(QPointF(50, 30)) is False  # centre: empty interior

    def test_line_hits_along_its_own_length(self):
        line = Line(colour=RED, stroke_width=4, start=QPointF(0, 0), end=QPointF(100, 0))

        assert line.hit_test(QPointF(50, 0)) is True  # midpoint
        assert line.hit_test(QPointF(50, 20)) is False  # well off the line

    def test_crop_hits_its_border_not_its_interior(self):
        # SNX-64: Crop gained its own hit_test override when it was
        # restored as a committable mark in overlay.py, mirroring
        # Rectangle's identical "stroked outline only" reasoning above --
        # unlike the base-class fallback it relied on before, exercised by
        # test_unrecognised_shape_is_never_hit below.
        crop = Crop(colour=RED, stroke_width=4, start=QPointF(10, 10), end=QPointF(60, 60))

        assert crop.hit_test(QPointF(10, 35)) is True  # left border
        assert crop.hit_test(QPointF(35, 35)) is False  # empty interior: not a hit

    def test_blur_and_pixelate_hit_their_whole_filled_rect(self):
        blur = Blur(colour=RED, stroke_width=4, start=QPointF(10, 10), end=QPointF(60, 60))
        pixelate = Pixelate(
            colour=RED, stroke_width=4, start=QPointF(10, 10), end=QPointF(60, 60)
        )

        for shape in (blur, pixelate):
            # Interior counts here, unlike a stroke-only shape: the whole
            # patch is visibly "the annotation."
            assert shape.hit_test(QPointF(35, 35)) is True
            assert shape.hit_test(QPointF(200, 200)) is False

    def test_step_marker_hits_within_its_radius(self):
        marker = StepMarker(colour=RED, stroke_width=4, point=QPointF(50, 50), number=1)

        assert marker.hit_test(QPointF(50, 50)) is True  # centre
        assert marker.hit_test(QPointF(50 + StepMarker.RADIUS - 1, 50)) is True  # just inside
        assert marker.hit_test(QPointF(200, 200)) is False

    def test_text_hits_near_its_anchor_point(self):
        text = Text(colour=RED, stroke_width=4, point=QPointF(50, 50), text="hi")

        assert text.hit_test(QPointF(50, 50)) is True
        assert text.hit_test(QPointF(500, 500)) is False

    def test_unrecognised_shape_is_never_hit(self):
        # Every concrete Shape shipped here now overrides hit_test (Crop
        # gained its own in SNX-64, the last one that hadn't), so the base
        # class's own safe default -- "a shape type without an override is
        # simply never a hit, even for a point squarely inside its
        # geometry" -- needs a throwaway subclass to exercise directly.
        @dataclass
        class _UnhandledShape(Shape):
            start: QPointF = field(default_factory=QPointF)
            end: QPointF = field(default_factory=QPointF)

            def draw(self, painter):
                pass

        shape = _UnhandledShape(colour=RED, stroke_width=4, start=QPointF(0, 0), end=QPointF(100, 100))

        assert shape.hit_test(QPointF(50, 50)) is False

    def test_a_dashed_line_is_hit_in_its_gaps(self):
        # The eraser aims at the line, not at its ink: a click between two
        # dashes still takes it.
        line = Line(
            colour=RED, stroke_width=2, start=QPointF(10, 50), end=QPointF(190, 50), dash="dashed"
        )

        assert line.hit_test(QPointF(22, 50)) is True  # 10 + a 9px dash: inside the first gap

    @pytest.mark.parametrize("shape_class", [Rectangle, Ellipse])
    def test_only_a_filled_shape_counts_its_interior_as_a_hit(self, shape_class):
        # `filled` hides what is under it, so its interior is the mark.
        # `both`'s faint tint does not, and keeps the outline-only rule.
        corners = dict(start=QPointF(10, 10), end=QPointF(90, 90))
        centre = QPointF(50, 50)

        assert shape_class(colour=RED, stroke_width=4, fill="filled", **corners).hit_test(centre) is True
        assert shape_class(colour=RED, stroke_width=4, fill="both", **corners).hit_test(centre) is False
        assert shape_class(colour=RED, stroke_width=4, **corners).hit_test(centre) is False


class TestFill:
    """#65: rectangles and ellipses outlined, filled or both. The fill is
    always the stroke colour, at the handoff's FILL_OPACITY."""

    CORNERS = dict(start=QPointF(10, 10), end=QPointF(90, 90))

    @pytest.mark.parametrize("fill", ["filled", "both"])
    @pytest.mark.parametrize("shape_class", [Rectangle, Ellipse])
    def test_the_interior_is_the_stroke_colour_at_the_handoffs_opacity(self, shape_class, fill):
        shape = shape_class(colour=TEAL, stroke_width=4, fill=fill, **self.CORNERS)

        result = render(make_image(), [shape])

        alpha = FILL_OPACITY[fill]
        centre = result.pixelColor(50, 50)
        for painted, ink in ((centre.red(), TEAL.red()), (centre.green(), TEAL.green()),
                             (centre.blue(), TEAL.blue())):
            assert painted == pytest.approx(ink * alpha + 255 * (1 - alpha), abs=2)

    @pytest.mark.parametrize("shape_class", [Rectangle, Ellipse])
    def test_both_keeps_its_outline_at_full_strength(self, shape_class):
        shape = shape_class(colour=RED, stroke_width=6, fill="both", **self.CORNERS)

        result = render(make_image(), [shape])

        assert result.pixelColor(10, 50) == RED  # the left edge, halfway down

    @pytest.mark.parametrize("shape_class", [Rectangle, Ellipse])
    def test_filled_has_no_outline(self, shape_class):
        # An outline straddles the edge, so half of it lands outside the
        # shape's bounds. A `filled` shape paints nothing there, and its
        # own edge is the tint rather than the full stroke colour.
        shape = shape_class(colour=RED, stroke_width=8, fill="filled", **self.CORNERS)

        result = render(make_image(), [shape])

        assert result.pixelColor(7, 50) == QColor(BACKGROUND)
        assert result.pixelColor(11, 50) != RED

    @pytest.mark.parametrize("style", [{"fill": "solid"}, {"dash": "dashes"}])
    def test_an_unknown_style_is_refused_when_the_mark_is_made(self, style):
        # Not left for draw() to find, inside a paintEvent, on every repaint.
        with pytest.raises(ValueError):
            Rectangle(colour=RED, stroke_width=4, **style)


class TestLineStyle:
    """#65: lines, arrows and shape outlines solid, dashed or dotted. The
    handoff's patterns are pixel lengths; QPen counts in pen widths."""

    @pytest.mark.parametrize("shape_class", [Rectangle, Ellipse, Line, Arrow])
    def test_the_default_is_todays_solid_pen(self, shape_class):
        shape = shape_class(colour=RED, stroke_width=4)

        pen = _line_pen(shape)

        assert shape.dash == "solid"
        assert getattr(shape, "fill", "outline") == "outline"
        assert pen.style() == Qt.PenStyle.SolidLine
        assert pen.capStyle() == Qt.PenCapStyle.RoundCap

    @pytest.mark.parametrize("dash", ["dashed", "dotted"])
    def test_a_gap_is_the_handoffs_length_at_any_stroke_width(self, dash):
        # Handed to QPen unconverted, a 7px gap would be 14px at a 2px
        # stroke and 84px at 12px. Measured along the centre of the line,
        # both widths must draw the handoff's own on and off lengths.
        on, off = DASH_PATTERNS[dash]
        measured = {}
        for width in (2, 12):
            line = Line(
                colour=RED, stroke_width=width, start=QPointF(10, 50), end=QPointF(190, 50), dash=dash
            )
            runs = ink_runs(render(make_image(size=(200, 100)), [line]), 50, 10, 190)
            measured[width] = runs[:-1]  # the last can be cut short by the line's end

        assert measured[2] == measured[12]
        assert set(measured[2]) == {(True, on), (False, off)}

    def test_solid_is_one_unbroken_run(self):
        line = Line(colour=RED, stroke_width=4, start=QPointF(10, 50), end=QPointF(190, 50))

        runs = ink_runs(render(make_image(size=(200, 100)), [line]), 50, 10, 190)

        assert runs == [(True, 180)]

    def test_a_rectangles_outline_takes_the_pattern(self):
        rect = Rectangle(
            colour=RED, stroke_width=2, start=QPointF(10, 20), end=QPointF(190, 80), dash="dashed"
        )

        runs = ink_runs(render(make_image(size=(200, 100)), [rect]), 20, 20, 180)

        on, off = DASH_PATTERNS["dashed"]
        assert set(runs[1:-1]) == {(True, on), (False, off)}  # both ends fall mid-dash

    def test_an_arrows_head_is_solid_under_a_dotted_shaft(self):
        start, end = QPointF(10, 50), QPointF(190, 50)
        dotted = render(
            make_image(size=(200, 100)),
            [Arrow(colour=RED, stroke_width=4, start=start, end=end, dash="dotted")],
        )
        solid = render(
            make_image(size=(200, 100)), [Arrow(colour=RED, stroke_width=4, start=start, end=end)]
        )

        # The shaft is broken...
        assert (False, DASH_PATTERNS["dotted"][1]) in ink_runs(dotted, 50, 10, 150)
        # ...and the head, from where it covers the shaft to its tip, is the
        # same filled triangle either way.
        head = QRect(180, 38, 12, 25)
        assert dotted.copy(head) == solid.copy(head)


class TestStyledExport:
    """#65: a mark's fill and line style export exactly as they paint."""

    def styled_marks(self):
        return [
            Rectangle(colour=RED, stroke_width=3, start=QPointF(30, 30), end=QPointF(110, 90),
                      fill="both", dash="dashed"),
            Ellipse(colour=BLUE, stroke_width=5, start=QPointF(60, 60), end=QPointF(170, 150),
                    fill="filled"),
            Line(colour=RED, stroke_width=2, start=QPointF(20, 170), end=QPointF(180, 170),
                 dash="dotted"),
            Arrow(colour=BLUE, stroke_width=4, start=QPointF(40, 120), end=QPointF(160, 40),
                  dash="dashed"),
        ]

    def test_export_matches_the_on_screen_render(self):
        # The overlay paints marks straight over the frame, in window
        # coordinates; the export maps them into the crop first. At 1:1 the
        # two must agree to the pixel, which holds only if that mapping
        # carries every mark's style across with its points.
        frame = Frame(
            image=make_image(size=(200, 200)), logical_origin=QPointF(0, 0),
            logical_size=QSizeF(200, 200),
        )
        selection = QRectF(10, 20, 180, 170)
        marks = self.styled_marks()

        on_screen = QImage(frame.image)
        painter = QPainter(on_screen)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        for mark in marks:
            mark.draw(painter)
        painter.end()

        exported = render_selection(frame, marks, selection)

        expected = on_screen.copy(selection.toRect())
        assert exported.convertToFormat(expected.format()) == expected

    def test_an_export_from_a_scaled_monitor_lays_its_dashes_where_the_screen_did(self):
        # On a 1.5x monitor the overlay paints in logical pixels and the
        # window's device makes them physical, so a 9px dash covers 13.5 of
        # the frame's pixels. The export must lay each dash over that same
        # stretch of the picture. Only where ink starts and stops along the
        # line is compared here; its width is TestExportedLengths' to check.
        image = make_image(size=(300, 150))
        frame = Frame(image=image, logical_origin=QPointF(0, 0), logical_size=QSizeF(200, 100))
        line = Line(
            colour=RED, stroke_width=2, start=QPointF(10, 50), end=QPointF(190, 50), dash="dashed"
        )

        on_screen = QImage(image)
        on_screen.setDevicePixelRatio(1.5)
        painter = QPainter(on_screen)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        line.draw(painter)
        painter.end()

        exported = render_selection(frame, [line], QRectF(0, 0, 200, 100))

        row = 75  # logical y=50, read in the frame's own pixels
        screen_edges = ink_edges(on_screen, row, 15, 285)
        export_edges = ink_edges(exported, row, 15, 285)
        assert len(screen_edges) == len(export_edges) > 10
        assert all(abs(seen - saved) <= 1 for seen, saved in zip(screen_edges, export_edges))


def half_max_width(image: QImage, pixels) -> int:
    """How many of `pixels` -- (x, y) pairs in `image`'s own pixels, running
    straight across a stroke on a white ground -- the ink darkens past half
    its own depth. That is the stroke's width however opaque its ink, and
    wherever its antialiased edges happen to fall.
    """
    greens = [image.pixelColor(x, y).green() for x, y in pixels]
    half = (max(greens) + min(greens)) / 2
    return sum(1 for green in greens if green < half)


def painted_extent(image: QImage) -> tuple[int, int, int]:
    """(pixels, width, height): how many pixels of a white image anything
    painted noticeably, and the size of the box they span."""
    xs, ys = [], []
    for y in range(image.height()):
        for x in range(image.width()):
            rgb = image.pixel(x, y)
            if min(qRed(rgb), qGreen(rgb), qBlue(rgb)) < 223:
                xs.append(x)
                ys.append(y)
    return len(xs), max(xs) - min(xs) + 1, max(ys) - min(ys) + 1


class TestExportedLengths:
    """#72: a mark exports at the size the screen drew it -- every length it
    paints with, not only its points.

    "The screen" is what the overlay's ink layer does: each mark's own
    draw() onto a device at a 1.5 pixel ratio, which turns its logical
    pixels physical. The export is render_selection over a frame holding
    those same physical pixels. Both are measured in physical pixels.
    """

    RATIO = 1.5
    LOGICAL = QSizeF(200, 120)
    PHYSICAL = (300, 180)

    def on_screen(self, mark: Shape, ratio: float = RATIO, size=PHYSICAL) -> QImage:
        image = make_image(size=size)
        image.setDevicePixelRatio(ratio)
        painter = QPainter(image)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        mark.draw(painter)
        painter.end()
        return image

    def frame(self, image: QImage | None = None) -> Frame:
        if image is None:
            image = make_image(size=self.PHYSICAL)
        return Frame(image=image, logical_origin=QPointF(0, 0), logical_size=self.LOGICAL)

    def exported(self, mark: Shape) -> QImage:
        return render_selection(self.frame(), [mark], QRectF(QPointF(0, 0), self.LOGICAL))

    # Each stroke, and a run of physical pixels straight across it: row 90
    # (logical y=60) through an upright stroke, or column 90 through the
    # arrow's level shaft.
    STROKES = {
        "line": (
            lambda width: Line(
                colour=RED, stroke_width=width, start=QPointF(100, 20), end=QPointF(100, 100)
            ),
            [(x, 90) for x in range(120, 180)],
        ),
        "rectangle": (
            lambda width: Rectangle(
                colour=RED, stroke_width=width, start=QPointF(40, 20), end=QPointF(160, 100)
            ),
            [(x, 90) for x in range(30, 90)],
        ),
        "ellipse": (
            lambda width: Ellipse(
                colour=RED, stroke_width=width, start=QPointF(40, 20), end=QPointF(160, 100)
            ),
            [(x, 90) for x in range(30, 90)],
        ),
        "pen": (
            lambda width: Pen(
                colour=RED, stroke_width=width, points=[QPointF(100, 20), QPointF(100, 100)]
            ),
            [(x, 90) for x in range(120, 180)],
        ),
        "highlighter": (
            lambda width: Highlighter(
                colour=RED, stroke_width=width, points=[QPointF(100, 20), QPointF(100, 100)]
            ),
            [(x, 90) for x in range(90, 210)],
        ),
        "arrow": (
            lambda width: Arrow(
                colour=RED, stroke_width=width, start=QPointF(20, 60), end=QPointF(180, 60)
            ),
            [(90, y) for y in range(60, 120)],
        ),
    }

    @pytest.mark.parametrize("width", [2, 5, 6, 12])
    @pytest.mark.parametrize("kind", list(STROKES))
    def test_a_stroke_exports_as_wide_as_the_screen_drew_it(self, kind, width):
        make, across = self.STROKES[kind]
        mark = make(width)
        logical_width = width * (Metric.HIGHLIGHT_MULT if kind == "highlighter" else 1)

        seen = half_max_width(self.on_screen(mark), across)
        saved = half_max_width(self.exported(mark), across)

        assert abs(seen - logical_width * self.RATIO) <= 1
        assert abs(saved - seen) <= 1

    @pytest.mark.parametrize("width", [2, 6])
    def test_an_arrowhead_exports_the_size_the_screen_drew_it(self, width):
        # At 2 the head is all floor, and a floor is a length in its own
        # right: scaling the stroke alone would leave it behind.
        arrow = Arrow(colour=RED, stroke_width=width, start=QPointF(20, 60), end=QPointF(180, 60))
        head_length = max(Arrow.HEAD_LENGTH_MIN, width * Arrow.HEAD_LENGTH_FACTOR)
        half_width = max(Arrow.HEAD_HALF_WIDTH_MIN, width * Arrow.HEAD_HALF_WIDTH_FACTOR)
        # Straight down through the head, a tenth of its length short of the base.
        x = round((180 - head_length * 0.9) * self.RATIO)
        down = [(x, y) for y in range(20, 160)]

        seen = half_max_width(self.on_screen(arrow), down)
        saved = half_max_width(self.exported(arrow), down)

        assert abs(seen - 2 * 0.9 * half_width * self.RATIO) <= 2
        assert abs(saved - seen) <= 1

    @pytest.mark.parametrize("width", [2, 5, 8])
    @pytest.mark.parametrize("kind", ["text", "step", "crop", "callout"])
    def test_a_label_badge_or_dashed_box_exports_the_size_the_screen_drew_it(self, kind, width):
        mark = {
            "text": lambda: Text(
                colour=RED, stroke_width=width, text="Label gy", point=QPointF(20, 20)
            ),
            "step": lambda: StepMarker(
                colour=BLUE, stroke_width=width, point=QPointF(100, 60), number=3
            ),
            "crop": lambda: Crop(
                colour=RED, stroke_width=width, start=QPointF(30, 20), end=QPointF(170, 100)
            ),
            "callout": lambda: Callout(
                colour=RED, stroke_width=width, start=QPointF(20, 20), end=QPointF(170, 100),
                text="Hi",
            ),
        }[kind]()

        seen_pixels, seen_width, seen_height = painted_extent(self.on_screen(mark))
        saved_pixels, saved_width, saved_height = painted_extent(self.exported(mark))

        # A font's pixel size is whole pixels, so at 1.5x a 15px label
        # exports at 22 where the screen drew 22.5: a pixel or two out,
        # where an unscaled label comes out a third smaller. That rounding
        # grows with the label, and hinting at the two sizes differs by
        # font: one Windows desk drew "Label gy" 84px on screen
        # and 79px exported. So the slack is a share of the size, still far
        # short of the third an unscaled export loses.
        assert abs(saved_width - seen_width) <= max(3, seen_width * 0.08)
        assert abs(saved_height - seen_height) <= max(3, seen_height * 0.08)
        # Its ink too -- but glyphs rasterised through a scaled painter and at
        # a larger pixel size are not the same pixels, and how far apart they
        # land depends on the font and the platform's rasteriser: up to 8% in
        # DejaVu Sans and 6.3% on Windows, where 6% had been allowed. A label
        # exported unscaled is 55% short, so 15% still catches it.
        assert abs(saved_pixels - seen_pixels) <= seen_pixels * 0.15

    @pytest.mark.parametrize("kind", [Blur, Pixelate, Spotlight])
    def test_blur_strength_stays_in_the_frames_own_pixels(self, kind):
        # The overlay bakes a blur (or a spotlight's dim) into the frozen
        # frame itself: the mark mapped into the frame's physical pixels,
        # strength as it stands. An export that scaled strength with the
        # lengths would come out blockier -- or, for a spotlight, dimmer or
        # fainter -- than the screen.
        image = make_gradient_image(size=self.PHYSICAL)
        mark = kind(
            colour=RED, stroke_width=4, start=QPointF(30, 20), end=QPointF(150, 90), strength=8
        )

        baked = replace(
            mark,
            start=QPointF(30 * self.RATIO, 20 * self.RATIO),
            end=QPointF(150 * self.RATIO, 90 * self.RATIO),
        ).apply(image)
        exported = render_selection(
            self.frame(image), [mark], QRectF(QPointF(0, 0), self.LOGICAL)
        )

        assert exported.convertToFormat(baked.format()) == baked

    def test_exporting_leaves_the_marks_themselves_at_screen_size(self):
        # The overlay keeps its marks and repaints them after an export; one
        # stretched in place would thicken the screen's ink every time.
        line = Line(colour=RED, stroke_width=6, start=QPointF(100, 20), end=QPointF(100, 100))
        before = self.on_screen(line)

        self.exported(line)

        assert line.length_scale == 1.0
        assert self.on_screen(line) == before

    @pytest.mark.parametrize("width", [2, 6])
    def test_at_1x_an_export_is_exactly_the_screens_pixels(self, width):
        # The crop's ratio is exactly 1.0 there, and a length times 1.0 is
        # itself. TestStyledExport covers the two-point shapes; these are
        # the marks whose other lengths -- a highlighter's widening, an
        # arrowhead, a label's type and chip, a badge -- now scale too.
        frame = Frame(
            image=make_image(size=(200, 120)), logical_origin=QPointF(0, 0),
            logical_size=QSizeF(200, 120),
        )
        selection = QRectF(10, 20, 180, 90)
        marks = [
            Pen(colour=RED, stroke_width=width,
                points=[QPointF(20, 30), QPointF(60, 80), QPointF(150, 40)]),
            Highlighter(colour=BLUE, stroke_width=width,
                        points=[QPointF(20, 90), QPointF(120, 50)]),
            Arrow(colour=BLUE, stroke_width=width, start=QPointF(30, 100), end=QPointF(170, 40)),
            Text(colour=RED, stroke_width=width, text="Hi gy", point=QPointF(30, 40)),
            StepMarker(colour=BLUE, stroke_width=width, point=QPointF(140, 60), number=2),
            Callout(colour=RED, stroke_width=width, start=QPointF(30, 20), end=QPointF(170, 100),
                    text="Hi"),
        ]

        for mark in marks:
            expected = self.on_screen(mark, ratio=1.0, size=(200, 120)).copy(selection.toRect())
            exported = render_selection(frame, [mark], selection)
            assert exported.convertToFormat(expected.format()) == expected, type(mark).__name__


class TestCalloutExportScaling:
    """The ticket's own "exported at 1.0 and 1.5 display scaling with the
    text the same size relative to its box in both": font size and the
    body's own size are both lengths (`Callout._font`, `TAIL_LENGTH`) that
    go through `_scaled` together with the crop's point-mapping, so their
    *ratio* should survive a change of scale even though neither one does
    on its own -- `render_selection`'s `_transformed` call is exercised
    directly here, the same one it uses internally, rather than a second,
    parallel scaling computation that could drift from it.
    """

    def test_ratios_match_between_1x_and_1_5x(self):
        callout = Callout(
            colour=RED, stroke_width=4, start=QPointF(20, 20), end=QPointF(160, 100), text="Hi",
        )

        def ratio_at(scale: float) -> float:
            mapped = _transformed(
                callout, lambda p: QPointF(p.x() * scale, p.y() * scale), scale
            )
            return mapped._font().pixelSize() / mapped.body_rect().height()

        assert ratio_at(1.5) == pytest.approx(ratio_at(1.0), rel=0.05)


# -- the watermark (#69) -------------------------------------------------------

MAGENTA = QColor(255, 0, 255)


def solid_image(width: int, height: int, colour: QColor = MAGENTA) -> QImage:
    image = QImage(width, height, QImage.Format.Format_ARGB32)
    image.fill(colour)
    return image


def colour_bounds(image: QImage, colour: QColor) -> QRect:
    """The smallest rect holding every pixel of `image` that is exactly
    `colour` -- a solid mark's extent, less whatever its edges blended --
    in the image's own pixels."""
    target = colour.rgb()
    xs, ys = [], []
    for y in range(image.height()):
        for x in range(image.width()):
            if image.pixel(x, y) == target:
                xs.append(x)
                ys.append(y)
    if not xs:
        return QRect()
    return QRect(min(xs), min(ys), max(xs) - min(xs) + 1, max(ys) - min(ys) + 1)


def assert_edges_near(found: QRect, expected: QRect, tolerance: int = 1) -> None:
    assert abs(found.left() - expected.left()) <= tolerance, (found, expected)
    assert abs(found.top() - expected.top()) <= tolerance, (found, expected)
    assert abs(found.right() - expected.right()) <= tolerance, (found, expected)
    assert abs(found.bottom() - expected.bottom()) <= tolerance, (found, expected)


class TestWatermark:
    """#69: a mark stamped once over a whole capture. Laid out by one rule,
    in logical pixels against the capture, for the preview and the export
    alike."""

    AREA = QRectF(0, 0, 400, 300)

    @pytest.mark.parametrize(
        "corner,left,top",
        [
            ("tl", 14, 14),
            ("tr", 400 - 14 - 40, 14),
            ("bl", 14, 300 - 14 - 20),
            ("br", 400 - 14 - 40, 300 - 14 - 20),
        ],
    )
    def test_each_corner_sits_the_inset_in_from_that_corner(self, corner, left, top):
        # A 400x300 capture's shorter side is 300, and 5% of it is under the
        # floor: the mark is 20 high, and a 2:1 image 40 wide.
        mark = Watermark(corner=corner, opacity=100, image=solid_image(200, 100))

        assert WATERMARK["inset"] == 14
        assert mark.rect(self.AREA) == QRectF(left, top, 40, 20)

    @pytest.mark.parametrize(
        "size,height",
        [((200, 200), WatermarkMetric.MARK_H_MIN), ((2000, 1000), 50), ((4000, 3000), WatermarkMetric.MARK_H_MAX)],
    )
    def test_its_height_follows_the_capture_between_a_floor_and_a_ceiling(self, size, height):
        mark = Watermark(corner="br", opacity=70, image=solid_image(1000, 1000))

        assert mark.rect(QRectF(0, 0, *size)).height() == pytest.approx(height)

    def test_an_image_is_never_stretched_past_its_own_pixels(self):
        mark = Watermark(corner="br", opacity=70, image=solid_image(30, 15))

        # 5% of a 1000px side is 50, and the image has 15 rows to give.
        assert mark.rect(QRectF(0, 0, 2000, 1000)).size() == QSizeF(30, 15)
        # On a 1.5x frame those 15 rows are 10 logical pixels, whichever space
        # the mark is painted in: the overlay's logical one...
        assert mark.rect(QRectF(0, 0, 2000, 1000), 1.0, 1.5).size() == QSizeF(20, 10)
        # ...or the export's physical one, where they are 15 again.
        assert mark.rect(QRectF(0, 0, 3000, 1500), 1.5).size() == QSizeF(30, 15)

    def test_a_wide_image_is_held_to_a_share_of_the_capture(self):
        mark = Watermark(corner="br", opacity=70, image=solid_image(2000, 100))

        rect = mark.rect(QRectF(0, 0, 800, 600))

        assert rect.width() == pytest.approx(800 * WatermarkMetric.MARK_W_SHARE)
        assert rect.width() / rect.height() == pytest.approx(20)

    def test_it_always_fits_inside_a_small_captures_insets(self):
        mark = Watermark(corner="tl", opacity=70, image=solid_image(100, 100))

        # 60x40 leaves 32x12 inside the insets, and the square shrinks to fit.
        assert mark.rect(QRectF(0, 0, 60, 40)) == QRectF(14, 14, 12, 12)

    def test_a_capture_with_no_room_inside_its_insets_gets_no_mark(self):
        mark = Watermark(corner="br", opacity=70, text="acme")

        assert mark.rect(QRectF(0, 0, 28, 200)) is None

    def test_the_inset_and_the_size_scale_into_physical_pixels(self):
        mark = Watermark(corner="br", opacity=70, image=solid_image(400, 200))

        logical = mark.rect(QRectF(0, 0, 400, 300))
        physical = mark.rect(QRectF(0, 0, 600, 450), 1.5)

        assert logical == QRectF(346, 266, 40, 20)
        assert physical == QRectF(519, 399, 60, 30)

    def test_a_text_mark_grows_with_its_height(self):
        text = "acme · internal"

        small = Watermark(corner="br", opacity=70, text=text).rect(self.AREA)
        large = Watermark(corner="br", opacity=70, text=text).rect(QRectF(0, 0, 2000, 1000))

        assert small.height() == WatermarkMetric.MARK_H_MIN
        assert large.height() == 50
        assert small.width() > small.height()
        assert large.width() / small.width() == pytest.approx(50 / 20, rel=0.15)

    def test_text_too_long_for_the_capture_is_elided_to_fit(self):
        mark = Watermark(corner="bl", opacity=70, text="an extremely long watermark " * 10)

        rect = mark.rect(QRectF(0, 0, 300, 200))

        assert rect.left() == 14
        assert rect.right() <= 300 - 14

    def test_a_text_mark_paints_its_plate_in_the_corner(self):
        image = make_image(size=(400, 300))
        mark = Watermark(corner="br", opacity=100, text="acme · internal")
        rect = mark.rect(self.AREA)

        painter = QPainter(image)
        mark.paint(painter, self.AREA)
        painter.end()

        # The plate's left end, clear of any glyph, is darker than the white
        # capture; outside the chip nothing changed.
        plate = image.pixelColor(round(rect.left()) + 2, round(rect.center().y()))
        assert plate.red() < 200
        assert image.pixelColor(round(rect.left()) - 4, round(rect.center().y())) == QColor(BACKGROUND)

    def test_a_corner_it_does_not_know_is_refused(self):
        with pytest.raises(ValueError):
            Watermark(corner="middle", opacity=70, text="acme")

    def test_an_opacity_outside_the_menus_range_is_refused(self):
        low, _high = WATERMARK["opacity_range"]

        with pytest.raises(ValueError):
            Watermark(corner="br", opacity=low - 1, text="acme")


class TestRenderSelectionWatermark:
    """#69: `render_selection` stamps the watermark once, above every mark."""

    MARK_CENTRE = (366, 276)  # of a 40x20 mark in a 400x300 capture's bottom right

    @staticmethod
    def _frame(image_size, logical_size) -> Frame:
        return Frame(
            image=make_image(size=image_size),
            logical_origin=QPointF(0, 0),
            logical_size=QSizeF(*logical_size),
        )

    def test_it_is_stamped_above_every_mark(self):
        frame = self._frame((400, 300), (400, 300))
        # A thick line straight through where the mark lands.
        line = Line(colour=BLUE, stroke_width=60, start=QPointF(300, 276), end=QPointF(400, 276))
        mark = Watermark(corner="br", opacity=100, image=solid_image(400, 200))

        exported = render_selection(frame, [line], QRectF(0, 0, 400, 300), watermark=mark)

        assert exported.pixelColor(*self.MARK_CENTRE) == MAGENTA
        assert exported.pixelColor(320, 276) == BLUE

    def test_without_one_nothing_is_stamped(self):
        frame = self._frame((400, 300), (400, 300))
        line = Line(colour=BLUE, stroke_width=6, start=QPointF(20, 20), end=QPointF(380, 280))
        selection = QRectF(0, 0, 400, 300)

        assert render_selection(frame, [line], selection, watermark=None) == render_selection(
            frame, [line], selection
        )

    def test_its_opacity_lets_the_capture_show_through(self):
        frame = self._frame((400, 300), (400, 300))
        mark = Watermark(corner="br", opacity=50, image=solid_image(400, 200))

        exported = render_selection(frame, [], QRectF(0, 0, 400, 300), watermark=mark)

        colour = exported.pixelColor(*self.MARK_CENTRE)
        assert (colour.red(), colour.blue()) == (255, 255)
        assert colour.green() == pytest.approx(128, abs=3)

    def test_it_sits_in_the_selections_corner_not_the_frames(self):
        frame = self._frame((600, 500), (600, 500))
        mark = Watermark(corner="tl", opacity=100, image=solid_image(400, 200))

        exported = render_selection(frame, [], QRectF(100, 150, 400, 300), watermark=mark)

        assert_edges_near(colour_bounds(exported, MAGENTA), QRect(14, 14, 40, 20))

    def test_a_scaled_export_stamps_it_where_the_screen_showed_it(self):
        # A 1.5x monitor: the frame holds one and a half pixels to a logical
        # one. The overlay paints in logical pixels on a device that makes
        # them physical; the export paints the crop's physical pixels.
        frame = self._frame((600, 450), (400, 300))
        mark = Watermark(corner="br", opacity=100, image=solid_image(400, 200))

        on_screen = make_image(size=(600, 450))
        on_screen.setDevicePixelRatio(1.5)
        painter = QPainter(on_screen)
        mark.paint(painter, QRectF(0, 0, 400, 300), 1.0, 1.5)
        painter.end()
        exported = render_selection(frame, [], QRectF(0, 0, 400, 300), watermark=mark)

        expected = QRect(519, 399, 60, 30)
        assert_edges_near(colour_bounds(on_screen, MAGENTA), expected)
        assert_edges_near(colour_bounds(exported, MAGENTA), expected)


class TestSnappedHighlighter:
    """A highlighter that snapped to text (`snipux.textsnap`) is its bands:
    they are what paints, what exports and what the eraser finds."""

    BAND = QRectF(20, 20, 60, 14)

    def _snapped(self, **changes) -> Highlighter:
        # Its points wander well outside the band, as a hand-drawn sweep does.
        shape = Highlighter(
            colour=RED,
            stroke_width=4,
            points=[QPointF(10, 60), QPointF(90, 5)],
            bands=[QRectF(self.BAND)],
        )
        return replace(shape, **changes)

    def test_it_paints_its_bands_and_not_its_sweep(self):
        image = QImage(100, 80, QImage.Format.Format_RGB32)
        image.fill(BACKGROUND)

        result = render(image, [self._snapped()])

        assert result.pixel(50, 27) != BACKGROUND
        # On the sweep's line, but outside the band.
        assert result.pixel(12, 58) == BACKGROUND

    def test_two_touching_bands_are_filled_once_where_they_meet(self):
        image = QImage(100, 80, QImage.Format.Format_RGB32)
        image.fill(BACKGROUND)
        overlapping = [QRectF(20, 20, 60, 14), QRectF(20, 30, 60, 14)]

        result = render(image, [self._snapped(bands=overlapping)])

        assert result.pixel(50, 32) == result.pixel(50, 24)

    def test_the_eraser_finds_it_on_its_band_and_not_on_its_sweep(self):
        shape = self._snapped()

        assert shape.hit_test(QPointF(50, 27))
        assert not shape.hit_test(QPointF(12, 58))

    def test_an_export_moves_its_bands_with_the_selection(self):
        # A 2x frame: the selection's origin comes off and every length
        # doubles, bands included.
        image = QImage(200, 160, QImage.Format.Format_RGB32)
        image.fill(BACKGROUND)
        frame = Frame(image=image, logical_origin=QPointF(0, 0), logical_size=QSizeF(100, 80))
        selection = QRectF(10, 10, 80, 60)

        result = render_selection(frame, [self._snapped()], selection)

        # The band's middle, (50, 27) on screen, is (80, 34) in the export.
        assert result.pixel(80, 34) != BACKGROUND
        assert result.pixel(4, 4) == BACKGROUND
