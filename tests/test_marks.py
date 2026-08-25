"""Tests for `snipux/marks.py` -- the mark model both annotatable surfaces
share.

`docs/design/handoff-windows.md` requires the review window's Annotate mode
to use "the same mark model" as the overlay. These test that model directly,
rather than only through one of its two owners.
"""

from __future__ import annotations

import pytest
from PyQt6.QtCore import QPointF
from PyQt6.QtGui import QColor
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
            ("blur", shapes.Blur),
        ],
    )
    def test_each_drag_tool_starts_its_own_shape(self, tool, expected):
        shape = begin_stroke(
            tool, QPointF(1, 1), colour=QColor("#fff"), stroke_width=4
        )

        assert isinstance(shape, expected)

    def test_pixelate_is_chosen_by_the_blur_mode(self):
        shape = begin_stroke(
            "blur", QPointF(1, 1), colour=QColor("#fff"), stroke_width=4,
            blur_mode="pixelate",
        )

        assert isinstance(shape, shapes.Pixelate)

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
