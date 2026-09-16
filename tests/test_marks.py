"""Tests for `snipux/marks.py` -- the mark model both annotatable surfaces
share.

`docs/design/handoff-windows.md` requires the review window's Annotate mode
to use "the same mark model" as the overlay. These test that model directly,
rather than only through one of its two owners.
"""

from __future__ import annotations

import pytest
from PyQt6.QtCore import QPointF, QRectF
from PyQt6.QtGui import QColor, QFont, QImage, QPainter
from PyQt6.QtWidgets import QApplication

from snipux import shapes
from snipux.design import tokens
from snipux.marks import (
    MarkStore,
    ToolStyle,
    ToolStyles,
    begin_stroke,
    extend_stroke,
    session_styles,
    snap_to_text,
)


@pytest.fixture(scope="module", autouse=True)
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def pen(x=0.0, y=0.0) -> shapes.Pen:
    return shapes.Pen(colour=QColor("#ffffff"), stroke_width=4, points=[QPointF(x, y)])


class TestHistory:
    def test_add_then_undo_removes_it(self):
        store = MarkStore()
        store.add(pen())

        store.undo()

        assert len(store) == 0
        assert store.can_redo

    def test_redo_puts_it_back(self):
        store = MarkStore()
        store.add(pen())
        store.undo()

        store.redo()

        assert len(store) == 1

    def test_a_new_mark_clears_the_redo_stack(self):
        # Whatever was undone becomes unreachable, same as any history.
        store = MarkStore()
        store.add(pen())
        store.undo()

        store.add(pen())

        assert not store.can_redo

    def test_undoing_an_erase_restores_its_draw_order(self):
        # The reason MarkAction carries an index: an erase removes from the
        # middle, so putting it back on the end would reorder the layer.
        store = MarkStore()
        first, second, third = pen(1), pen(2), pen(3)
        for shape in (first, second, third):
            store.add(shape)
        store._marks.pop(1)
        store._undo.append(type(store._undo[0])("erase", 1, second))

        store.undo()

        assert store.marks == (first, second, third)

    def test_clear_is_one_undoable_step(self):
        store = MarkStore()
        store.add(pen())
        store.add(pen())

        assert store.clear() is True
        assert len(store) == 0

        store.undo()
        assert len(store) == 2

    def test_clearing_nothing_is_not_a_step(self):
        store = MarkStore()

        assert store.clear() is False
        assert not store.can_undo

    def test_reset_leaves_no_way_back(self):
        # Esc's discard, deliberately outside the history.
        store = MarkStore()
        store.add(pen())

        store.reset()

        assert len(store) == 0
        assert not store.can_undo and not store.can_redo

    def test_undo_and_redo_with_nothing_to_do_are_no_ops(self):
        store = MarkStore()
        store.undo()
        store.redo()

        assert len(store) == 0

    def test_a_batch_is_one_undo_step(self):
        store = MarkStore()
        earlier = pen(0)
        store.add(earlier)

        store.add_all([pen(1), pen(2), pen(3)])
        assert len(store) == 4

        store.undo()
        assert store.marks == (earlier,)

    def test_redo_puts_a_batch_back_in_place_and_order(self):
        store = MarkStore()
        earlier = pen(0)
        batch = [pen(1), pen(2), pen(3)]
        store.add(earlier)
        store.add_all(batch)
        store.undo()

        store.redo()

        assert store.marks == (earlier, *batch)

    def test_undoing_a_batch_leaves_marks_added_before_it(self):
        # The trap: undo() used to send every kind it did not name to the
        # clear branch, which would have wiped the whole list.
        store = MarkStore()
        first, second = pen(1), pen(2)
        store.add(first)
        store.add(second)
        store.add_all([pen(3)])

        store.undo()

        assert store.marks == (first, second)

    def test_an_empty_batch_is_not_a_step(self):
        store = MarkStore()
        fired = []
        store.changed.connect(lambda: fired.append(True))

        store.add_all([])

        assert not store.can_undo
        assert fired == []

    def test_a_batch_clears_the_redo_stack_and_emits_once(self):
        store = MarkStore()
        store.add(pen())
        store.undo()
        fired = []
        store.changed.connect(lambda: fired.append(True))

        store.add_all([pen(1), pen(2)])

        assert not store.can_redo
        assert len(fired) == 1

    def test_changed_fires_once_per_mutation(self):
        store = MarkStore()
        fired = []
        store.changed.connect(lambda: fired.append(True))

        store.add(pen())
        store.undo()
        store.redo()
        store.clear()

        assert len(fired) == 4


class TestErase:
    def test_erase_takes_the_topmost_mark(self):
        # Draw order is paint order, so an overlap resolves to the visible
        # one.
        store = MarkStore()
        lower = shapes.Rectangle(
            colour=QColor("#fff"), stroke_width=4,
            start=QPointF(0, 0), end=QPointF(100, 100),
        )
        upper = shapes.Rectangle(
            colour=QColor("#fff"), stroke_width=4,
            start=QPointF(0, 0), end=QPointF(100, 100),
        )
        store.add(lower)
        store.add(upper)

        assert store.erase(QPointF(0, 50)) is upper

    def test_a_miss_removes_nothing_and_raises_nothing(self):
        store = MarkStore()
        store.add(pen())

        assert store.erase(QPointF(9999, 9999)) is None
        assert len(store) == 1


class TestStrokeFactory:
    """One factory for both surfaces -- a tool added to one is a tool both
    get.
    """

    @pytest.mark.parametrize(
        "tool,expected",
        [
            ("pen", shapes.Pen),
            ("highlighter", shapes.Highlighter),
            ("arrow", shapes.Arrow),
            ("rect", shapes.Rectangle),
            ("ellipse", shapes.Ellipse),
            ("line", shapes.Line),
            ("callout", shapes.Callout),
            ("blur", shapes.Blur),
            ("pixelate", shapes.Pixelate),
            ("blackout", shapes.Blackout),
        ],
    )
    def test_each_drag_tool_starts_its_own_shape(self, tool, expected):
        shape = begin_stroke(
            tool, QPointF(1, 1), colour=QColor("#fff"), stroke_width=4
        )

        assert type(shape) is expected

    def test_a_redaction_takes_the_strength_it_is_given(self):
        shape = begin_stroke(
            "pixelate", QPointF(1, 1), colour=QColor("#fff"), stroke_width=4,
            blur_strength=13,
        )

        assert shape.strength == 13

    @pytest.mark.parametrize("tool", ["step", "text", "eraser", None])
    def test_tools_with_no_drag_gesture_start_nothing(self, tool):
        # step commits on the click, text opens an editor, the eraser
        # removes rather than adds.
        assert begin_stroke(tool, QPointF(1, 1), colour=QColor("#fff"), stroke_width=4) is None

    def test_a_freehand_stroke_grows_by_points(self):
        shape = begin_stroke("pen", QPointF(0, 0), colour=QColor("#fff"), stroke_width=4)

        extend_stroke(shape, QPointF(10, 10))

        assert shape.points[-1] == QPointF(10, 10)

    def test_a_two_point_stroke_grows_by_its_end(self):
        shape = begin_stroke("rect", QPointF(0, 0), colour=QColor("#fff"), stroke_width=4)

        extend_stroke(shape, QPointF(10, 10))

        assert shape.end == QPointF(10, 10)

    @pytest.mark.parametrize("tool", ["rect", "ellipse", "callout"])
    def test_a_closed_shape_takes_a_fill_and_a_line_style(self, tool):
        shape = begin_stroke(
            tool, QPointF(1, 1), colour=QColor("#fff"), stroke_width=4, fill="both", dash="dotted"
        )

        assert (shape.fill, shape.dash) == ("both", "dotted")

    @pytest.mark.parametrize("tool", ["line", "arrow"])
    def test_a_line_takes_a_line_style_and_no_fill(self, tool):
        shape = begin_stroke(
            tool, QPointF(1, 1), colour=QColor("#fff"), stroke_width=4, fill="filled", dash="dashed"
        )

        assert shape.dash == "dashed"
        assert not hasattr(shape, "fill")

    @pytest.mark.parametrize("tool", ["pen", "highlighter", "blur"])
    def test_a_tool_with_nothing_to_style_ignores_a_style(self, tool):
        # A remembered per-tool style carries both keys whether the tool
        # uses them or not, so being handed one must not break the tool.
        shape = begin_stroke(
            tool, QPointF(1, 1), colour=QColor("#fff"), stroke_width=4, fill="filled", dash="dashed"
        )

        assert shape is not None
        assert not hasattr(shape, "fill") and not hasattr(shape, "dash")

    @pytest.mark.parametrize("tool", ["rect", "ellipse", "line", "arrow", "callout"])
    def test_no_style_given_is_todays_look(self, tool):
        shape = begin_stroke(tool, QPointF(1, 1), colour=QColor("#fff"), stroke_width=4)

        assert shape.dash == "solid"
        assert getattr(shape, "fill", "outline") == "outline"


class TestStyledMarks:
    """#65: fill and line style belong to the mark -- through undo, redo and
    erase, and whatever the tool is set to next."""

    @staticmethod
    def rect(**style) -> shapes.Rectangle:
        return shapes.Rectangle(
            colour=QColor("#ef4444"), stroke_width=3,
            start=QPointF(10, 10), end=QPointF(80, 60), **style,
        )

    @staticmethod
    def picture(store: MarkStore) -> QImage:
        base = QImage(100, 100, QImage.Format.Format_RGB32)
        base.fill(QColor("#ffffff"))
        return shapes.render(base, list(store.marks))

    def test_undo_then_redo_brings_a_mark_back_in_its_own_style(self):
        store = MarkStore()
        store.add(self.rect(fill="both", dash="dashed"))
        before = self.picture(store)

        store.undo()
        assert self.picture(store) != before
        store.redo()

        assert (store.marks[0].fill, store.marks[0].dash) == ("both", "dashed")
        assert self.picture(store) == before

    def test_undoing_an_erase_brings_a_mark_back_in_its_own_style(self):
        store = MarkStore()
        store.add(self.rect(fill="filled", dash="dotted"))
        before = self.picture(store)

        assert store.erase(QPointF(40, 30)) is not None
        store.undo()

        assert self.picture(store) == before

    def test_a_filled_mark_is_erased_ahead_of_the_mark_it_hides(self):
        # Erase walks back to front so a click takes the mark you can see.
        # A stroke under a filled box cannot be seen.
        store = MarkStore()
        hidden = shapes.Pen(
            colour=QColor("#000000"), stroke_width=4, points=[QPointF(20, 30), QPointF(70, 30)]
        )
        box = self.rect(fill="filled")
        store.add(hidden)
        store.add(box)

        assert store.erase(QPointF(40, 30)) is box
        assert store.marks == (hidden,)

    def test_the_next_mark_in_another_style_leaves_the_last_one_alone(self):
        store = MarkStore()
        first = begin_stroke(
            "rect", QPointF(10, 10), colour=QColor("#fff"), stroke_width=4,
            fill="filled", dash="dotted",
        )
        extend_stroke(first, QPointF(60, 60))
        store.add(first)

        second = begin_stroke("rect", QPointF(20, 20), colour=QColor("#fff"), stroke_width=4)
        extend_stroke(second, QPointF(70, 70))
        store.add(second)

        assert (store.marks[0].fill, store.marks[0].dash) == ("filled", "dotted")
        assert (store.marks[1].fill, store.marks[1].dash) == ("outline", "solid")


class TestToolStyles:
    """#68: style is per tool and remembered, first seeded from the
    handoff's `DEFAULT_STYLE`."""

    @pytest.mark.parametrize("tool", list(tokens.DEFAULT_STYLE))
    def test_each_tool_starts_from_the_handoffs_seed(self, tool):
        seed = tokens.DEFAULT_STYLE[tool]

        style = ToolStyles().of(tool)

        assert (style.colour, style.size, style.dash, style.fill) == (
            seed["color"], seed["size"], seed["dash"], seed["fill"]
        )

    @pytest.mark.parametrize("tool", ["blur", "pixelate", "blackout", "eraser", None])
    def test_a_tool_the_seed_leaves_out_starts_where_the_spec_starts_it(self, tool):
        other = tokens.DEFAULT_STYLE_OTHER

        style = ToolStyles().of(tool)

        assert (style.colour, style.size) == (other["color"], other["size"])
        assert style.strength == tokens.Metric.BLUR_DEFAULT

    def test_a_dashed_red_box_leaves_the_pen_alone(self):
        styles = ToolStyles()
        pen = styles.of("pen")

        styles.update("rect", colour="#ff0000", dash="dashed", size=9)

        assert styles.of("pen") == pen
        assert (styles.of("rect").colour, styles.of("rect").dash) == ("#ff0000", "dashed")

    def test_blur_and_pixelate_keep_their_own_strength(self):
        styles = ToolStyles()

        styles.update("blur", strength=18)

        assert styles.of("blur").strength == 18
        assert styles.of("pixelate").strength == tokens.Metric.BLUR_DEFAULT

    def test_size_and_strength_stay_inside_their_ranges(self):
        styles = ToolStyles()
        low, high = tokens.STROKE_RANGE
        weakest, strongest = tokens.STRENGTH_RANGE

        assert styles.update("pen", size=high + 40).size == high
        assert styles.update("pen", size=low - 40).size == low
        assert styles.update("blur", strength=strongest + 40).strength == strongest
        assert styles.update("blur", strength=weakest - 40).strength == weakest

    def test_a_style_handed_out_cannot_be_changed_behind_the_store(self):
        styles = ToolStyles()

        with pytest.raises(AttributeError):
            styles.of("pen").colour = "#000000"

    def test_reset_goes_back_to_the_seed(self):
        styles = ToolStyles()
        styles.update("pen", colour="#123456", size=20)

        styles.reset()

        assert styles.of("pen") == ToolStyles().of("pen")

    def test_the_session_has_one(self):
        # What the overlay and the review window both draw with, so a style
        # set on one snip is still set on the next.
        assert isinstance(session_styles, ToolStyles)
        assert isinstance(session_styles.of("pen"), ToolStyle)


class TestSnapToText:
    """The one place a highlighter's sweep meets `textsnap`, for the overlay
    and the review window alike -- and the only code here that converts
    between a mark's space and an image's pixels."""

    @staticmethod
    def _page(scale: int = 1) -> QImage:
        image = QImage(400 * scale, 60 * scale, QImage.Format.Format_RGB32)
        image.fill(QColor("#ffffff"))
        painter = QPainter(image)
        painter.scale(scale, scale)
        font = QFont()
        font.setPixelSize(18)
        painter.setFont(font)
        painter.setPen(QColor("#202020"))
        painter.drawText(QPointF(20, 36), "several words of text")
        painter.end()
        return image

    @staticmethod
    def _sweep() -> shapes.Highlighter:
        return shapes.Highlighter(
            colour=QColor("#f59e0b"),
            stroke_width=5,
            points=[QPointF(x, 31 + (x % 3)) for x in range(30, 200, 6)],
        )

    def test_a_sweep_over_text_comes_back_with_bands_and_its_points_kept(self):
        sweep = self._sweep()

        snapped = snap_to_text(sweep, self._page())

        assert len(snapped.bands) == 1
        assert snapped.points == sweep.points
        assert snapped.colour == sweep.colour

    def test_a_sweep_over_nothing_comes_back_as_it_was(self):
        blank = QImage(400, 60, QImage.Format.Format_RGB32)
        blank.fill(QColor("#ffffff"))
        sweep = self._sweep()

        assert snap_to_text(sweep, blank) is sweep

    def test_anything_but_a_highlighter_comes_back_as_it_was(self):
        stroke = pen(30, 31)
        stroke.points.append(QPointF(200, 31))

        assert snap_to_text(stroke, self._page()) is stroke

    def test_bands_come_back_in_the_marks_space_not_the_images(self):
        # The overlay on a 2x display: marks in logical pixels over a frame
        # with twice as many. The band is where it is at 1x, not twice as far.
        (at_one,) = snap_to_text(self._sweep(), self._page()).bands
        (at_two,) = snap_to_text(self._sweep(), self._page(scale=2), 2.0, 2.0).bands

        for single, double in (
            (at_one.left(), at_two.left()),
            (at_one.top(), at_two.top()),
            (at_one.right(), at_two.right()),
            (at_one.bottom(), at_two.bottom()),
        ):
            assert abs(single - double) <= 2


class TestSnapStyle:
    def test_the_highlighter_starts_snapping(self):
        assert ToolStyles().of("highlighter").snap == "text"

    def test_switching_the_highlighter_to_freehand_leaves_its_colour(self):
        styles = ToolStyles()
        before = styles.of("highlighter")

        styles.update("highlighter", snap="free")

        assert styles.of("highlighter") == ToolStyle(
            colour=before.colour, size=before.size, dash=before.dash, fill=before.fill, snap="free"
        )
