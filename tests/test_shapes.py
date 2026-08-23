import pytest
from PyQt6.QtCore import QPointF
from PyQt6.QtGui import QColor, QFontMetrics, QImage, qRgb
from PyQt6.QtWidgets import QApplication

from snipux.shapes import (
    Arrow,
    Ellipse,
    Highlighter,
    Line,
    Pen,
    Rectangle,
    StepMarker,
    Text,
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
