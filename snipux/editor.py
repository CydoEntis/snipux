"""The annotation window.

Per CLAUDE.md's one architectural rule, everything here is ordinary drawing
on the frozen `Frame` the overlay already captured and cropped — no code
path in this module asks the compositor for pixels. `Canvas` displays that
frozen image, maps between widget-local and image-pixel coordinates, and
owns the shape list and mouse handling for the drawing and markup tools
defined in `shapes.py`; `Editor` wraps it with a toolbar for picking a tool,
colour, and stroke width.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from PyQt6.QtCore import QEvent, QPointF, QRectF, Qt
from PyQt6.QtGui import QAction, QActionGroup, QColor, QPainter
from PyQt6.QtWidgets import (
    QLineEdit,
    QSpinBox,
    QToolBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from snipux.app import copy_image_to_clipboard, save_image
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
    Shape,
    StepMarker,
    Text,
    apply_crop,
    render,
)


class Tool(Enum):
    """Which shape a canvas drag creates. Mirrors `SelectionMode` in
    overlay.py stylistically: chosen via a named setter, never a bare
    attribute write.
    """

    PEN = "pen"
    HIGHLIGHTER = "highlighter"
    ARROW = "arrow"
    LINE = "line"
    RECTANGLE = "rectangle"
    ELLIPSE = "ellipse"
    TEXT = "text"
    STEP_MARKER = "step_marker"
    BLUR = "blur"
    PIXELATE = "pixelate"
    CROP = "crop"
    # Appended after the existing members, not inserted earlier: Editor.__init__
    # arms next(iter(Tool)) as the startup default, and that must stay PEN.


# One shape class per tool. The freehand tools (pen/highlighter) build from
# an ordered point list; the rest build from two endpoints — see
# Canvas._new_in_progress_shape/mouseMoveEvent below for how each
# in-progress shape grows.
_FREEHAND_SHAPE_CLASSES = {
    Tool.PEN: Pen,
    Tool.HIGHLIGHTER: Highlighter,
}
_TWO_POINT_SHAPE_CLASSES = {
    Tool.ARROW: Arrow,
    Tool.LINE: Line,
    Tool.RECTANGLE: Rectangle,
    Tool.ELLIPSE: Ellipse,
    Tool.BLUR: Blur,
    Tool.PIXELATE: Pixelate,
    Tool.CROP: Crop,
}


@dataclass(frozen=True)
class _HistoryState:
    """One committed (frame, shapes) snapshot in Canvas's undo/redo stack.

    Holds the same `Frame`/`Shape` objects that were live at commit time,
    not copies — see Canvas._push_history's docstring for why that's safe.
    """

    frame: Frame
    shapes: tuple[Shape, ...]


class Canvas(QWidget):
    """Displays a `Frame`'s image centered, letterboxed, and never upscaled.

    Built from a `Frame` (not a bare `QImage`), mirroring `Overlay` in
    overlay.py — the editor hands this whatever `Frame` the overlay
    confirmed (already cropped to the user's selection via `Frame.crop()`),
    and future tickets (crop tool, redraw-after-annotate) will want the
    `Frame`'s logical geometry, not just its pixels.

    Like `Overlay`'s `_to_local`/`_to_absolute`, coordinate-space helpers
    here are small, named, pure functions computed on demand from current
    widget geometry — there is no cached scale or target-rect state to
    invalidate on resize, just one source of truth (`self.rect()` +
    `self.image.size()`) recomputed every call.
    """

    DEFAULT_COLOUR = QColor(Qt.GlobalColor.black)
    DEFAULT_STROKE_WIDTH = 3

    def __init__(self, frame: Frame, parent=None):
        super().__init__(parent)
        self._frame = frame

        # Confirmed shapes, in draw order — what render() consumes. No tool
        # is armed until a caller (Editor's toolbar) calls set_tool(), same
        # as Overlay starting with no selection mode-specific state set.
        self._shapes: list[Shape] = []
        self._tool: Tool | None = None
        self._colour: QColor = QColor(self.DEFAULT_COLOUR)
        self._stroke_width: float = self.DEFAULT_STROKE_WIDTH

        # The shape being built by an in-progress drag, or None between
        # drags. Lives outside self._shapes until mouseReleaseEvent commits
        # it, so a paintEvent mid-drag can show it without it being final.
        self._in_progress_shape: Shape | None = None

        # Text placement: unlike every other tool, text commits from a
        # QLineEdit rather than a drag (see mousePressEvent/_commit_text).
        # Created lazily on first use and reused after, per PLAN.md.
        self._text_edit: QLineEdit | None = None
        self._pending_point: QPointF | None = None
        self._pending_colour: QColor | None = None
        self._pending_stroke_width: float | None = None
        # Guards against QLineEdit.editingFinished firing twice for a single
        # Enter press (once for Enter, once for the focus loss _commit_text's
        # own hide() call causes) — see _commit_text.
        self._committing_text = False

        # Undo/redo stack: entry zero is the empty-canvas starting state, not
        # a special-cased "nothing to undo" sentinel — that's what makes
        # undo()'s guard a plain index-zero check rather than an is-empty one.
        self._history: list[_HistoryState] = [_HistoryState(self._frame, ())]
        self._history_index = 0

    @property
    def image(self):
        return self._frame.image

    @property
    def shapes(self) -> tuple[Shape, ...]:
        """Confirmed shapes in draw order. A copy, not the live list, so a
        caller can't mutate this canvas's state by reaching into it —
        mirrors Overlay exposing selection state via signals/properties
        rather than a bare attribute.
        """
        return tuple(self._shapes)

    def _push_history(self) -> None:
        """Commit the current (frame, shapes) as a new undo/redo entry.

        Called at the four points below that actually mutate self._frame/
        self._shapes, and only once the mutation has happened — a no-op
        drag (degenerate crop, empty text, no tool armed) must stay a no-op
        for history too, not push a snapshot identical to the last one.

        Stores the live `self._frame` object and a tuple of the live shape
        objects, not deep copies. That's safe only because nothing mutates a
        committed shape or frame afterwards: a dragged shape is built via
        _new_in_progress_shape as a throwaway instance and nothing holds a
        mutating reference to it once mouseReleaseEvent appends it;
        apply_crop() (shapes.py) always builds a brand-new Frame rather than
        writing through the old one. The one apparent exception is
        StepMarker.number, which render() reassigns on every paint call —
        but nothing ever trusts a stored .number, so a stale value from a
        different history entry's last paint is always overwritten before
        it's next drawn. See PLAN.md for the fuller argument.
        """
        del self._history[self._history_index + 1 :]  # drop stale redo entries
        self._history.append(_HistoryState(self._frame, tuple(self._shapes)))
        self._history_index += 1

    def _restore_history_state(self) -> None:
        state = self._history[self._history_index]
        self._frame = state.frame
        self._shapes = list(state.shapes)
        self.update()

    def undo(self) -> None:
        if self._history_index == 0:
            return  # already at the empty-canvas starting state
        self._history_index -= 1
        self._restore_history_state()

    def redo(self) -> None:
        if self._history_index >= len(self._history) - 1:
            return  # nothing ahead: no undo has happened, or redo already caught up
        self._history_index += 1
        self._restore_history_state()

    def set_tool(self, tool: Tool | None) -> None:
        self._tool = tool

    def set_colour(self, colour: QColor) -> None:
        self._colour = QColor(colour)

    def set_stroke_width(self, stroke_width: float) -> None:
        self._stroke_width = stroke_width

    def _target_rect(self) -> QRectF:
        """Where self.image is drawn in widget-local coordinates: centered,
        scaled to fit self.rect() without exceeding 1.0 (never upscaled),
        preserving aspect ratio. Recomputed from current widget size and
        image size on every call — nothing cached to invalidate on resize.
        """
        image_size = self.image.size()
        widget_size = self.size()
        if image_size.width() <= 0 or image_size.height() <= 0:
            return QRectF()  # degenerate image: nothing to draw, no target

        scale = min(
            widget_size.width() / image_size.width(),
            widget_size.height() / image_size.height(),
            1.0,  # the "never upscale past 100%" rule
        )
        target_w = image_size.width() * scale
        target_h = image_size.height() * scale
        x = (widget_size.width() - target_w) / 2
        y = (widget_size.height() - target_h) / 2
        return QRectF(x, y, target_w, target_h)

    def widget_to_image(self, point: QPointF) -> QPointF | None:
        """Widget-local point -> image-pixel point, or None if point falls
        in the letterbox margin outside the drawn image.
        """
        target = self._target_rect()
        if target.width() <= 0 or target.height() <= 0 or not target.contains(point):
            return None
        scale = target.width() / self.image.width()
        local = point - target.topLeft()
        return QPointF(local.x() / scale, local.y() / scale)

    def image_to_widget(self, point: QPointF) -> QPointF:
        """Image-pixel point -> widget-local point. Total (no None case):
        callers hold image-space points that came from inside the image by
        construction, unlike widget-space points which can land anywhere
        including the margin.

        Guards the same degenerate-image case widget_to_image guards via
        _target_rect()'s 0x0 fallback, so this can't divide by zero the way
        an unguarded version would (self.image.width() == 0 and
        target.width() == 0.0 together, if this weren't checked first).
        There's no meaningful scale to invert for an empty image, so this
        returns target.topLeft() (itself (0, 0) in that case) rather than
        raising — degrading the same way widget_to_image degrades to None,
        just without a None case to return through.
        """
        target = self._target_rect()
        if target.width() <= 0 or target.height() <= 0 or self.image.width() <= 0:
            return target.topLeft()
        scale = target.width() / self.image.width()
        return target.topLeft() + QPointF(point.x() * scale, point.y() * scale)

    # -- drawing tools ----------------------------------------------------
    # In-progress-drag state, mouse handling, and painting all use image-
    # pixel coordinates (via widget_to_image) except where noted, matching
    # shapes.py's coordinate-space convention.

    def _new_in_progress_shape(self, anchor: QPointF) -> Shape | None:
        if self._tool in _FREEHAND_SHAPE_CLASSES:
            shape_class = _FREEHAND_SHAPE_CLASSES[self._tool]
            return shape_class(
                colour=QColor(self._colour),
                stroke_width=self._stroke_width,
                points=[anchor],
            )
        if self._tool in _TWO_POINT_SHAPE_CLASSES:
            shape_class = _TWO_POINT_SHAPE_CLASSES[self._tool]
            return shape_class(
                colour=QColor(self._colour),
                stroke_width=self._stroke_width,
                start=anchor,
                end=anchor,
            )
        return None  # no tool armed

    def _ensure_text_edit(self) -> QLineEdit:
        if self._text_edit is None:
            self._text_edit = QLineEdit(self)
            self._text_edit.hide()
            self._text_edit.editingFinished.connect(self._commit_text)
        return self._text_edit

    def _commit_text(self) -> None:
        # Re-entrancy guard, not a signal disconnect: hide() below drops the
        # field's focus and re-triggers editingFinished synchronously. The
        # flag is already True at that point, so the re-entrant call returns
        # here before touching self._shapes again. A disconnect would need
        # an explicit reconnect (easy to forget, and the field is reused
        # across placements) so this needs none.
        if self._committing_text:
            return
        self._committing_text = True
        try:
            if self._text_edit.text():
                self._shapes.append(
                    Text(
                        colour=self._pending_colour,
                        stroke_width=self._pending_stroke_width,
                        point=self._pending_point,
                        text=self._text_edit.text(),
                    )
                )
                self._push_history()
            self._text_edit.hide()
            self._pending_point = None
            self.update()
        finally:
            self._committing_text = False

    def mousePressEvent(self, event) -> None:
        if event.button() != Qt.MouseButton.LeftButton or self._tool is None:
            return
        image_point = self.widget_to_image(event.position())
        if image_point is None:
            return  # press landed in the letterbox margin: a no-op, like Overlay

        if self._tool is Tool.STEP_MARKER:
            self._shapes.append(
                StepMarker(
                    colour=QColor(self._colour),
                    stroke_width=self._stroke_width,
                    point=image_point,
                )
            )
            self._push_history()
            self.update()
            return  # one marker per click; never touches _in_progress_shape

        if self._tool is Tool.TEXT:
            self._pending_point = image_point
            self._pending_colour = QColor(self._colour)
            self._pending_stroke_width = self._stroke_width
            text_edit = self._ensure_text_edit()
            text_edit.clear()
            text_edit.move(event.position().toPoint())
            text_edit.show()
            text_edit.setFocus()
            return  # commits later via editingFinished, not here

        self._in_progress_shape = self._new_in_progress_shape(image_point)

    def mouseMoveEvent(self, event) -> None:
        if self._in_progress_shape is None:
            return
        image_point = self.widget_to_image(event.position())
        if image_point is None:
            return  # dragged into the margin: hold the shape where it was
        if isinstance(self._in_progress_shape, (Pen, Highlighter)):
            self._in_progress_shape.points.append(image_point)
        else:
            self._in_progress_shape.end = image_point
        self.update()

    def mouseReleaseEvent(self, event) -> None:
        if event.button() != Qt.MouseButton.LeftButton or self._in_progress_shape is None:
            return
        shape = self._in_progress_shape
        self._in_progress_shape = None

        if isinstance(shape, Crop):
            # Crop doesn't join the shape list like every other tool: it
            # flattens what's there and replaces the base image outright —
            # see apply_crop()'s docstring. A degenerate drag (no area) is a
            # no-op rather than replacing the frame with an empty image,
            # same spirit as the letterbox-margin no-ops above.
            crop_rect = QRectF(shape.start, shape.end).normalized()
            if crop_rect.width() > 0 and crop_rect.height() > 0:
                self._frame = apply_crop(self._frame, self._shapes, crop_rect)
                self._shapes = []
                self._push_history()
            self.update()
            return

        self._shapes.append(shape)
        self._push_history()
        self.update()

    def _visible_shapes(self) -> list[Shape]:
        """Confirmed shapes plus the in-progress drag, if any — the single
        list both the live preview and (implicitly, once confirmed) the
        final image are rendered from. See PLAN.md's note on why paintEvent
        has exactly one rendering path instead of a separate preview draw.
        """
        if self._in_progress_shape is None:
            return list(self._shapes)
        return [*self._shapes, self._in_progress_shape]

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), self.palette().window())
        flattened = render(self.image, self._visible_shapes())
        painter.drawImage(self._target_rect(), flattened)
        painter.end()  # never left open across a pixmap read, per CLAUDE.md


class Editor(QWidget):
    """Outer window: a toolbar (tool, colour, stroke width) above `Canvas`.

    The object the app will eventually construct instead of a bare `Canvas`
    once a later ticket wires the whole capture-to-save flow together; this
    ticket only adds the widget itself; nothing outside this module builds
    one yet.
    """

    # A fixed preset row, not a colour picker — per PLAN.md, a QColorDialog
    # (or any modal) is out of scope for this ticket so no control here can
    # ever block a test (or a user) on a dialog. The exact palette is an
    # implementation detail, not an acceptance criterion.
    SWATCH_COLOURS = [
        QColor(Qt.GlobalColor.black),
        QColor(Qt.GlobalColor.red),
        QColor(Qt.GlobalColor.green),
        QColor(Qt.GlobalColor.blue),
        QColor(Qt.GlobalColor.yellow),
    ]

    MIN_STROKE_WIDTH = 1
    MAX_STROKE_WIDTH = 20

    def __init__(self, frame: Frame, parent=None):
        super().__init__(parent)
        self.canvas = Canvas(frame, self)

        # Matches the Windows Snipping Tool workflow: the snip lands on the
        # clipboard the instant it's confirmed, before any annotation is
        # made — done here, right after Canvas is built and before any
        # toolbar-building call, so no shape can exist yet when this runs.
        copy_image_to_clipboard(frame.image)

        toolbar = QToolBar(self)
        self.tool_actions: dict[Tool, QAction] = {}
        self._build_tool_actions(toolbar)
        self.colour_buttons: dict[str, QToolButton] = {}
        self._build_colour_swatches(toolbar)
        self.stroke_width_spinbox = self._build_stroke_width_control(toolbar)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(toolbar)
        layout.addWidget(self.canvas)

        # Start from a usable state (a tool armed, a colour and width set)
        # rather than requiring a click before the first drag does anything.
        default_tool = next(iter(Tool))
        self.tool_actions[default_tool].setChecked(True)
        self.canvas.set_tool(default_tool)
        self.canvas.set_colour(self.SWATCH_COLOURS[0])
        self.canvas.set_stroke_width(self.stroke_width_spinbox.value())

    def _build_tool_actions(self, toolbar: QToolBar) -> None:
        group = QActionGroup(toolbar)
        group.setExclusive(True)
        for tool in Tool:
            action = QAction(tool.value.capitalize(), toolbar)
            action.setCheckable(True)
            # Default arg binds `tool` at definition time, not call time —
            # without it every action's handler would close over whichever
            # `tool` the loop last landed on.
            action.triggered.connect(
                lambda checked, tool=tool: self.canvas.set_tool(tool)
            )
            group.addAction(action)
            toolbar.addAction(action)
            self.tool_actions[tool] = action

    def _build_colour_swatches(self, toolbar: QToolBar) -> None:
        for colour in self.SWATCH_COLOURS:
            button = QToolButton(toolbar)
            button.setStyleSheet(f"background-color: {colour.name()};")
            button.setToolTip(colour.name())
            button.clicked.connect(
                lambda checked=False, colour=colour: self.canvas.set_colour(colour)
            )
            toolbar.addWidget(button)
            self.colour_buttons[colour.name()] = button

    def _build_stroke_width_control(self, toolbar: QToolBar) -> QSpinBox:
        spinbox = QSpinBox(toolbar)
        spinbox.setRange(self.MIN_STROKE_WIDTH, self.MAX_STROKE_WIDTH)
        spinbox.setValue(Canvas.DEFAULT_STROKE_WIDTH)
        spinbox.valueChanged.connect(self.canvas.set_stroke_width)
        toolbar.addWidget(spinbox)

        # The spinbox keeps Qt's default StrongFocus, and its internal
        # QLineEdit claims Ctrl+Z/Ctrl+Shift+Z for its own (always-empty)
        # text-undo history during Qt's ShortcutOverride pass — before a
        # QKeyEvent would ever reach keyPressEvent below to bubble up from
        # it. So "set a stroke width, then Ctrl+Z" would silently do nothing
        # without this: installed on both the spinbox and its line-edit
        # child, since production delivers the real key event to the line
        # edit (the spinbox's focus proxy) while a test that targets the
        # spinbox widget directly lands on the spinbox itself. The text
        # tool's QLineEdit is deliberately left alone — Ctrl+Z undoing a
        # keystroke there while it has focus is reasonable, not a bug.
        spinbox.installEventFilter(self)
        for child in spinbox.children():
            if isinstance(child, QLineEdit):
                child.installEventFilter(self)
        return spinbox

    def undo(self) -> None:
        self.canvas.undo()

    def redo(self) -> None:
        self.canvas.redo()

    def _undo_redo_action(self, key, modifiers) -> str | None:
        """Return "redo", "undo", or None for a given key/modifiers pair.
        Shared by keyPressEvent and eventFilter so the two dispatch paths
        can't drift.

        Checks the Ctrl+Shift+Z case first: its modifier set includes
        ControlModifier, so a bitwise "is Control held" check written before
        the Shift check would swallow every redo as an undo. Exact equality
        against the combined flag (not a bitwise subset test) is
        load-bearing here, not a style choice.
        """
        if key != Qt.Key.Key_Z:
            return None
        if modifiers == (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier):
            return "redo"
        if modifiers == Qt.KeyboardModifier.ControlModifier:
            return "undo"
        return None

    def eventFilter(self, watched, event) -> bool:
        # Only the stroke-width spinbox (and its line-edit child) install
        # this filter — see _build_stroke_width_control. Intercepting at
        # ShortcutOverride, not KeyPress, is what lets this run before the
        # line edit's own event() gets a chance to accept the key for its
        # internal text-undo.
        if event.type() == QEvent.Type.ShortcutOverride:
            action = self._undo_redo_action(event.key(), event.modifiers())
            if action == "redo":
                event.accept()
                self.redo()
                return True
            if action == "undo":
                event.accept()
                self.undo()
                return True
        return super().eventFilter(watched, event)

    def _save(self) -> None:
        """Save the currently-rendered, annotated image — not the raw
        capture — to the default location, without prompting for a name.
        Built from Canvas's public `shapes` property (a confirmed-shapes
        copy), not `Canvas._visible_shapes()`, which also includes an
        in-progress drag: that's the paint-preview's concern, not the save
        contract's.
        """
        rendered = render(self.canvas.image, list(self.canvas.shapes))
        save_image(rendered)

    def keyPressEvent(self, event) -> None:
        # Exact-equality modifier check, not bitwise, for the same reason
        # _undo_redo_action uses one for Ctrl+Shift+Z: a bitwise "Control
        # held" test would also match Ctrl+Shift+S, reserved for a possible
        # future "save as" this ticket doesn't add.
        if (
            event.key() == Qt.Key.Key_S
            and event.modifiers() == Qt.KeyboardModifier.ControlModifier
        ):
            self._save()
            return

        action = self._undo_redo_action(event.key(), event.modifiers())
        if action == "redo":
            self.redo()
        elif action == "undo":
            self.undo()
        else:
            super().keyPressEvent(event)
