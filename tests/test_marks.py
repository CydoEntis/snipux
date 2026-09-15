"""Tests for `snipux/marks.py` -- the mark model both annotatable surfaces
share.

`docs/design/handoff-windows.md` requires the review window's Annotate mode
to use "the same mark model" as the overlay. These test that model directly,
rather than only through one of its two owners.
"""

from __future__ import annotations

import pytest
from PyQt6.QtCore import QPointF
from PyQt6.QtGui import QColor, QImage
from PyQt6.QtWidgets import QApplication

from snipux import shapes
from snipux.marks import MarkStore, begin_stroke, extend_stroke


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
            ("crop", shapes.Crop),
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

    @pytest.mark.parametrize("tool", ["rect", "ellipse"])
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

    @pytest.mark.parametrize("tool", ["pen", "highlighter", "crop", "blur"])
    def test_a_tool_with_nothing_to_style_ignores_a_style(self, tool):
        # A remembered per-tool style carries both keys whether the tool
        # uses them or not, so being handed one must not break the tool.
        shape = begin_stroke(
            tool, QPointF(1, 1), colour=QColor("#fff"), stroke_width=4, fill="filled", dash="dashed"
        )

        assert shape is not None
        assert not hasattr(shape, "fill") and not hasattr(shape, "dash")

    @pytest.mark.parametrize("tool", ["rect", "ellipse", "line", "arrow"])
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
