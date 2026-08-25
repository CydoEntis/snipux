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

from PyQt6.QtCore import QObject, QPointF, Qt, pyqtSignal
from PyQt6.QtWidgets import QLineEdit

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

    def erase(self, point: QPointF, slack: float = 0.0) -> Shape | None:
        """Remove and return the topmost mark under `point`, or None.

        Back to front, so an overlap resolves to the visible one. A miss
        removes nothing and raises nothing -- not every click lands on ink.

        `slack` widens the click into a small ring of probes, in the same
        units the marks are in. A shape's hit tolerance is fixed in document
        units, so on a view that scales the document down -- the review
        window at anything under 100% -- it shrinks to a couple of screen
        pixels and a thin outline becomes almost unclickable. Callers that
        scale pass the slack that restores it.

        Two passes, and the order is the point. Outlines first, all of them,
        so clicking near a mark inside a box still takes the mark. Only when
        nothing anywhere was hit does the second pass consider enclosed
        areas, so clicking the empty middle of a box takes the box rather
        than doing nothing at all -- which is what made the eraser look like
        it only worked on freehand strokes.
        """
        probes = [point]
        if slack > 0:
            probes += [
                QPointF(point.x() + dx * slack, point.y() + dy * slack)
                for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1),
                               (0.7, 0.7), (-0.7, 0.7), (0.7, -0.7), (-0.7, -0.7))
            ]

        for test in (lambda s, pt: s.hit_test(pt), lambda s, pt: s.interior_hit_test(pt)):
            for index in range(len(self._marks) - 1, -1, -1):
                shape = self._marks[index]
                if any(test(shape, probe) for probe in probes):
                    self._marks.pop(index)
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


class LabelLineEdit(QLineEdit):
    """The text tool's own label editor -- a
    plain `QLineEdit` except for one thing: it accepts the Return/Enter key
    event it already consumed (SNX-76).

    Stock `QLineEdit.keyPressEvent` deliberately leaves Return/Enter
    unaccepted after emitting `returnPressed`/`editingFinished`, precisely
    so a dialog's default button can still fire from inside a text field.
    Neither host window has a default button, only the same key bound to
    "copy and dismiss" (`keyPressEvent`'s own Enter branch) -- and Qt
    propagates an unaccepted key event up the parent-widget chain, so the
    very keystroke that just committed this label as a mark (`_commit_text`,
    wired to `editingFinished`) would otherwise reach `OverlayWindow.
    keyPressEvent` a second time and fire that shortcut too, closing the
    overlay the user only meant to add one label to. `_shortcuts_suppressed`
    can't catch this second delivery: `_commit_text` already hid the field
    and dropped its focus by the time the event arrives there. Accepting the
    event here, once the base class is done with it, is what stops that
    second delivery from happening at all.
    """

    def keyPressEvent(self, event) -> None:
        super().keyPressEvent(event)
        if event.key() in (Qt.Key.Key_Enter, Qt.Key.Key_Return):
            event.accept()


class TextLabelEditor(QObject):
    """The text tool's click-to-type label, for any widget that hosts marks.

    Extracted from `OverlayWindow` for the same reason the bar and the store
    were: the review window's Edit mode needs the identical gesture -- click,
    type, click elsewhere or press Enter to commit -- and a second
    implementation of a re-entrancy-guarded commit is exactly the drift the
    design forbids.

    `host` is the widget the field is placed on. `to_document` converts a
    host-widget point into whatever space marks are stored in: identity for
    the overlay, which keeps both in window coordinates, and a zoom-aware
    mapping for the review window, whose marks live in image coordinates.
    """

    def __init__(self, host, store: "MarkStore", to_document=None):
        # Parented to the host, and deliberately keeping no Python
        # reference back to it. `self._host = host` would close a reference
        # cycle through the widget's own __dict__, which defers its
        # collection to a GC pass rather than dropping it the moment the
        # last reference goes -- and a still-mapped overlay that outlives
        # the code that made it goes on receiving mouse events meant for
        # its successor. `parent()` is a C++ pointer and costs nothing.
        super().__init__(host)
        self._store = store
        self._to_document = to_document or (lambda point: point)
        self._field: LabelLineEdit | None = None
        self._point: QPointF | None = None
        self._colour = None
        self._stroke_width: float | None = None
        self._committing = False

    @property
    def field(self) -> "LabelLineEdit | None":
        """The live field, or None before the first label. Callers check it
        to decide whether a key belongs to a focused label.
        """
        return self._field

    def is_active(self) -> bool:
        return self._field is not None and self._field.isVisible()

    def begin(self, point: QPointF, colour, stroke_width: float) -> None:
        """Open a label at `point` (host coordinates), seeded empty and
        focused for immediate typing.

        Any label already open commits first, against its *own* point and
        colour rather than this click's -- a click elsewhere never blurs the
        field, so nothing else would force the `editingFinished` the commit
        hangs off, and the previous label would be lost.
        """
        if self._field is not None:
            self.commit()
        self._point = self._to_document(point)
        self._colour = colour
        self._stroke_width = stroke_width
        field = self._ensure_field()
        field.clear()
        field.move(point.toPoint())
        field.show()
        field.setFocus()

    def _ensure_field(self) -> "LabelLineEdit":
        if self._field is None:
            self._field = LabelLineEdit(self.parent())
            # Styled to match the chip it becomes. Unstyled, a QLineEdit
            # paints its palette's opaque base -- a black rectangle sitting
            # over the screenshot, which is what it looked like: a bug, not
            # a text field. The tokens here are the same ones `shapes.Text`
            # paints its committed chip with, so typing looks like what you
            # get.
            from . import design as _design

            background = _design.color("TEXT_LABEL_BG")
            ring = _design.color("TEXT_LABEL_RING")
            metric = _design.tokens.Metric
            self._field.setStyleSheet(
                "QLineEdit {"
                f" background: rgba({background.red()}, {background.green()},"
                f" {background.blue()}, {background.alphaF():.2f});"
                f" border: {metric.TEXT_LABEL_RING_W}px solid"
                f" rgba({ring.red()}, {ring.green()}, {ring.blue()},"
                f" {ring.alphaF():.2f});"
                f" border-radius: {metric.TEXT_LABEL_RADIUS}px;"
                f" padding: {metric.TEXT_LABEL_PAD_V}px {metric.TEXT_LABEL_PAD_H}px;"
                f" color: {_design.color('TEXT_PRIMARY').name()};"
                " selection-background-color: rgba(255, 255, 255, 0.22); }"
            )
            # Grey hint text, not a seeded value -- `commit`'s own emptiness
            # guard is what makes a label nobody typed into cost nothing.
            self._field.setPlaceholderText("Label")
            self._field.hide()
            self._field.editingFinished.connect(self.commit)
        return self._field

    def commit(self) -> None:
        """Turn whatever was typed into a `Text` mark, or nothing if the
        field is empty.
        """
        # A re-entrancy guard, not a signal disconnect: hide() below drops
        # focus and re-fires editingFinished synchronously.
        if self._committing or self._field is None:
            return
        self._committing = True
        try:
            if self._field.text():
                self._store.add(
                    shapes_module.Text(
                        colour=self._colour,
                        stroke_width=self._stroke_width,
                        point=self._point,
                        text=self._field.text(),
                    )
                )
            self._field.hide()
            self._point = None
        finally:
            self._committing = False

    def abandon(self) -> None:
        """Escape's first stage while a label is focused: empty and hide the
        field so nothing is committed.

        Emptied *before* hiding, because hiding a focused field fires
        `editingFinished` -- and `commit`'s emptiness guard is then what
        makes that firing harmless.
        """
        if self._field is None:
            return
        self._field.clear()
        self._field.hide()
