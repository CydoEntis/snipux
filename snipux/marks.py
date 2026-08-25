"""The mark model and its undo/redo history, shared by every surface that
can be annotated.

`docs/design/handoff-windows.md` is explicit that the review window's
Annotate mode uses "the same mark model" as the overlay and "must not become
a second editor". This module is what makes that literally true: the overlay
and the review window both drive one `MarkStore`, so a fix to erase ordering
or a change to what `clear` means lands in both by construction rather than
by remembering to.

Deliberately Qt-light and geometry-agnostic. The store knows nothing about
selections, images, zoom or coordinate spaces -- a `Shape`'s points are in
whatever space its owner draws in (window coordinates for the overlay, image
coordinates for the review window, per the design) and the store never looks
at them except to hit-test, which the shapes do themselves.
"""

from __future__ import annotations

from dataclasses import dataclass

from PyQt6.QtCore import QObject, QPointF, pyqtSignal

from . import shapes as shapes_module
from .shapes import Shape


@dataclass(frozen=True)
class MarkAction:
    """One entry in a `MarkStore`'s history.

    `kind` is `'add'` for a mark that was appended, `'erase'` for one that
    was removed, or `'clear'` for the whole list emptied in one step.
    `index` is the position in draw order the action happened at -- unused
    for `'clear'`, which always empties and restores the entire list.
    `shape` is the single `Shape` for `'add'`/`'erase'`, or the tuple of
    every mark that was on screen for `'clear'`.

    Carrying the index is what lets an undone erase go back exactly where
    it was rather than on the end: an erase can remove from the middle of
    the list, unlike an add, which always appends.
    """

    kind: str
    index: int
    shape: object


class MarkStore(QObject):
    """The ink layer: an ordered list of marks plus one undo/redo history
    that every kind of change takes its turn in.

    Draw order is paint order, and `erase` walks it back to front so an
    overlap resolves to whichever mark is actually visible at that pixel.

    `changed` fires on every mutation. Owners connect it to whatever they
    need to keep in step -- a repaint, the floating bar's undo/redo
    buttons, a dirty flag in a footer -- rather than each mutating method
    knowing about any of them.
    """

    changed = pyqtSignal()

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._marks: list[Shape] = []
        self._undo: list[MarkAction] = []
        self._redo: list[MarkAction] = []

    # -- reading ---------------------------------------------------------

    @property
    def marks(self) -> tuple[Shape, ...]:
        """Contents in paint order. A copy, so a caller iterating cannot be
        surprised by a mutation mid-loop.
        """
        return tuple(self._marks)

    def __len__(self) -> int:
        return len(self._marks)

    def __bool__(self) -> bool:
        return bool(self._marks)

    @property
    def can_undo(self) -> bool:
        return bool(self._undo)

    @property
    def can_redo(self) -> bool:
        return bool(self._redo)

    # -- mutating --------------------------------------------------------

    def add(self, shape: Shape) -> None:
        """Append `shape` and clear the redo stack.

        A mark committed after an undo makes whatever was undone
        unreachable by redo, the same as any ordinary history.
        """
        self._undo.append(MarkAction("add", len(self._marks), shape))
        self._marks.append(shape)
        self._redo.clear()
        self.changed.emit()

    def erase(self, point: QPointF) -> Shape | None:
        """Remove and return the topmost mark under `point`, or None.

        Back to front, so an overlap resolves to the visible one. A miss
        removes nothing and raises nothing -- not every click lands on ink,
        and that is not an error.
        """
        for index in range(len(self._marks) - 1, -1, -1):
            if self._marks[index].hit_test(point):
                shape = self._marks.pop(index)
                self._undo.append(MarkAction("erase", index, shape))
                self._redo.clear()
                self.changed.emit()
                return shape
        return None

    def clear(self) -> bool:
        """Empty the list as a single undoable step. False if there was
        nothing to clear, so a caller knows whether to toast.

        Clear takes its turn in the general history rather than dropping
        everything outright: it sits in the same bar as undo and redo, and
        is the most destructive button in the tool.
        """
        if not self._marks:
            return False
        self._undo.append(MarkAction("clear", 0, tuple(self._marks)))
        self._marks = []
        self._redo.clear()
        self.changed.emit()
        return True

    def reset(self) -> None:
        """Drop every mark and both stacks outright, with no way back.

        Esc's discard, not the bar's clear: a leave-immediately gesture,
        not a button sitting next to undo where a mis-aimed click is easy.
        """
        self._marks = []
        self._undo = []
        self._redo = []
        self.changed.emit()

    def undo(self) -> None:
        """Invert the newest action and move it to the redo stack.

        An 'add' is undone by removing that mark; an 'erase' by reinserting
        it at the index it was removed from; a 'clear' by restoring every
        mark it carries in its original draw order. A no-op with nothing to
        undo.
        """
        if not self._undo:
            return
        action = self._undo.pop()
        if action.kind == "add":
            self._marks.pop(action.index)
        elif action.kind == "erase":
            self._marks.insert(action.index, action.shape)
        else:
            self._marks = list(action.shape)
        self._redo.append(action)
        self.changed.emit()

    def redo(self) -> None:
        """Replay the newest undone action. The exact mirror of `undo`."""
        if not self._redo:
            return
        action = self._redo.pop()
        if action.kind == "add":
            self._marks.insert(action.index, action.shape)
        elif action.kind == "erase":
            self._marks.pop(action.index)
        else:
            self._marks = []
        self._undo.append(action)
        self.changed.emit()


# Tool name -> Shape subclass. Kept here rather than in overlay.py because
# the review window's Annotate mode builds marks from the very same tool
# names -- the design's "one tool set, one mark model, two places it can
# appear". A tool added to one surface is a tool both surfaces get.
FREEHAND_TOOLS = {"pen": shapes_module.Pen, "highlighter": shapes_module.Highlighter}
TWO_POINT_TOOLS = {
    "arrow": shapes_module.Arrow,
    "rect": shapes_module.Rectangle,
    "ellipse": shapes_module.Ellipse,
    "line": shapes_module.Line,
    "crop": shapes_module.Crop,
}


def begin_stroke(
    tool: str | None,
    point: QPointF,
    *,
    colour,
    stroke_width: int,
    step_number: int = 1,
    blur_mode: str = "blur",
    blur_strength: int | None = None,
) -> Shape | None:
    """The shape a press with `tool` starts, or None if that tool has no
    drag gesture (`step` commits on the click; `text` opens an editor; the
    eraser removes rather than adds).

    `point` is in whatever space the caller draws in -- window coordinates
    for the overlay, image coordinates for the review window. The shape
    neither knows nor cares, which is what lets one factory serve both.
    """
    if tool in FREEHAND_TOOLS:
        return FREEHAND_TOOLS[tool](
            colour=colour, stroke_width=stroke_width, points=[point]
        )
    if tool in TWO_POINT_TOOLS:
        return TWO_POINT_TOOLS[tool](
            colour=colour, stroke_width=stroke_width, start=point, end=point
        )
    if tool == "blur":
        shape_class = (
            shapes_module.Blur if blur_mode == "blur" else shapes_module.Pixelate
        )
        extra = {} if blur_strength is None else {"strength": blur_strength}
        return shape_class(
            colour=colour, stroke_width=stroke_width, start=point, end=point, **extra
        )
    return None


def extend_stroke(shape: Shape, point: QPointF) -> None:
    """Grow an in-progress `shape` to `point`.

    Freehand strokes append a point; every other shape moves its `end`.
    Both classes share those field names, so this one split covers all of
    them.
    """
    if isinstance(shape, (shapes_module.Pen, shapes_module.Highlighter)):
        shape.points.append(point)
    else:
        shape.end = point
