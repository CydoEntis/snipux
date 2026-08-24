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

from PyQt6.QtCore import QEvent, QPointF, QRectF, Qt, pyqtSignal
from PyQt6.QtGui import (
    QAction,
    QActionGroup,
    QColor,
    QPainter,
    QPainterPath,
    QPainterPathStroker,
    QPen,
)
from PyQt6.QtWidgets import (
    QColorDialog,
    QLineEdit,
    QMenu,
    QSpinBox,
    QToolBar,
    QToolButton,
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
    ERASER = "eraser"
    # Appended after the existing members, not inserted earlier: Editor.__init__
    # arms next(iter(Tool)) as the startup default, and that must stay PEN.


def _tool_label(tool: Tool) -> str:
    """Human-facing text for `tool`'s toolbar action.

    `Tool.value` is a lowercase, underscore-separated identifier chosen for
    the data model (STEP_MARKER = "step_marker"), not for display -- shown
    as-is it reads like a variable name that leaked into the UI. This turns
    any tool's value into an ordinary title-cased label instead, per SNX-26.
    """
    return tool.value.replace("_", " ").title()


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

# Eraser hit-testing lives here, not in shapes.py: it's Canvas interaction
# logic (deciding which shape a click lands on), not part of the annotation
# data model or the flattening renderer those classes serve.
#
# A fixed image-pixel slack added on top of a shape's own stroke width, so a
# thin 1px line or a precisely-placed point (Text, StepMarker) stays
# genuinely clickable without requiring pixel-perfect aim.
_ERASER_HIT_TOLERANCE = 6.0


def _stroked(path: QPainterPath, width: float) -> QPainterPath:
    stroker = QPainterPathStroker()
    stroker.setWidth(width)
    return stroker.createStroke(path)


def _eraser_hit_path(shape: Shape) -> QPainterPath:
    """The fillable region a click on `shape` must land in for the eraser to
    pick it, in image-pixel coordinates.

    Stroke-only shapes (pen/highlighter/line/arrow/rectangle/ellipse) hit-
    test against their outline widened by stroke width plus
    `_ERASER_HIT_TOLERANCE`, via `QPainterPathStroker` — mirroring what
    render() actually paints. Clicking the empty interior of an unfilled
    rectangle is clicking whatever is behind it, not the rectangle, so that
    interior deliberately does not count as a hit.

    Filled shapes (step markers, blur/pixelate patches) hit-test against
    their actual filled area instead, since the whole area is visibly "the
    annotation" there. Text hit-tests against a small fixed box around its
    anchor point — good enough to pick it out without duplicating this
    module's font-metrics sizing logic here.

    Returns an empty path (never contains a point) for a shape type this
    doesn't recognise, which is the safe default for an eraser: a shape it
    can't reason about should never be silently removed.
    """
    path = QPainterPath()
    stroke_width = max(shape.stroke_width, 1.0) + _ERASER_HIT_TOLERANCE

    if isinstance(shape, (Pen, Highlighter)):
        if not shape.points:
            return path  # no segment yet to hit-test against
        path.moveTo(shape.points[0])
        for point in shape.points[1:]:
            path.lineTo(point)
        return _stroked(path, stroke_width)

    if isinstance(shape, (Line, Arrow)):
        path.moveTo(shape.start)
        path.lineTo(shape.end)
        return _stroked(path, stroke_width)

    if isinstance(shape, (Rectangle, Ellipse)):
        rect = QRectF(shape.start, shape.end).normalized()
        if isinstance(shape, Rectangle):
            path.addRect(rect)
        else:
            path.addEllipse(rect)
        return _stroked(path, stroke_width)

    if isinstance(shape, (Blur, Pixelate)):
        path.addRect(QRectF(shape.start, shape.end).normalized())
        return path

    if isinstance(shape, StepMarker):
        radius = max(StepMarker.MIN_RADIUS, shape.stroke_width * StepMarker.RADIUS_FACTOR)
        path.addEllipse(shape.point, radius, radius)
        return path

    if isinstance(shape, Text):
        half_extent = _ERASER_HIT_TOLERANCE + shape.stroke_width
        path.addRect(
            QRectF(
                shape.point.x() - half_extent,
                shape.point.y() - half_extent,
                half_extent * 2,
                half_extent * 2,
            )
        )
        return path

    return path


@dataclass(frozen=True)
class _HistoryState:
    """One committed (frame, shapes) snapshot in Canvas's undo/redo stack.

    Holds the same `Frame`/`Shape` objects that were live at commit time,
    not copies — see Canvas._push_history's docstring for why that's safe.
    """

    frame: Frame
    shapes: tuple[Shape, ...]


class Canvas(QWidget):
    """Displays a `Frame`'s image at 1:1, filling the widget exactly.

    Built from a `Frame` (not a bare `QImage`), mirroring `Overlay` in
    overlay.py — the editor hands this whatever `Frame` the overlay
    confirmed (already cropped to the user's selection via `Frame.crop()`),
    and future tickets (crop tool, redraw-after-annotate) will want the
    `Frame`'s logical geometry, not just its pixels.

    Per SNX-21, `Editor` sizes this widget to exactly the frame's logical
    size (see `Editor._position_over_snip`), so there is no fit-to-widget
    scaling or letterbox margin to compute here any more — a click always
    lands on the image, never on dead space around it.

    Like `Overlay`'s `_to_local`/`_to_absolute`, coordinate-space helpers
    here are small, named, pure functions computed on demand from current
    widget geometry — there is no cached scale or target-rect state to
    invalidate on resize, just one source of truth (`self.rect()` +
    `self.image.size()`) recomputed every call.
    """

    # Red, not black (SNX-25): black strokes on a dark capture (e.g. a
    # terminal screenshot) are invisible until a colour is deliberately
    # chosen, which read as "drawing is broken" rather than "pick a colour."
    # Red is legible against both light and dark captures and is the
    # conventional annotation colour, so it's a safe first mark either way.
    DEFAULT_COLOUR = QColor(Qt.GlobalColor.red)
    DEFAULT_STROKE_WIDTH = 3

    # Emitted whenever _history/_history_index actually changes (a pushed
    # entry, or an undo/redo that moved the index) — never for a guarded
    # no-op, so a toolbar listening for this to refresh Undo/Redo enabled
    # state never redraws on a click that did nothing.
    history_changed = pyqtSignal()

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

    @property
    def colour(self) -> QColor:
        """The colour that will be used for the next annotation. A copy, not
        the live QColor, for the same reason `shapes` returns a copy — so
        Editor's colour-picker (SNX-25) can read the current colour to seed
        the dialog without reaching into a private attribute.
        """
        return QColor(self._colour)

    def _push_history(self) -> None:
        """Commit the current (frame, shapes) as a new undo/redo entry.

        Called at the points below that actually mutate self._frame/
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
        self.history_changed.emit()

    def _restore_history_state(self) -> None:
        state = self._history[self._history_index]
        self._frame = state.frame
        self._shapes = list(state.shapes)
        self.update()
        self.history_changed.emit()

    @property
    def can_undo(self) -> bool:
        return self._history_index > 0

    @property
    def can_redo(self) -> bool:
        return self._history_index < len(self._history) - 1

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

    def clear(self) -> None:
        """Discard every confirmed annotation as a single undo step: one
        undo() afterwards restores every shape that was live, per SNX-22.

        A no-op when there's nothing to clear (mirrors the no-op guards
        elsewhere that feed _push_history — a degenerate crop, empty text):
        pushing an identical history entry would still count as a step to
        undo through without changing anything, which is worse than not
        offering the click at all.
        """
        if not self._shapes:
            return
        self._shapes = []
        self._push_history()
        self.update()

    def set_tool(self, tool: Tool | None) -> None:
        self._tool = tool

    def set_colour(self, colour: QColor) -> None:
        self._colour = QColor(colour)

    def set_stroke_width(self, stroke_width: float) -> None:
        self._stroke_width = stroke_width

    def _target_rect(self) -> QRectF:
        """Where self.image is drawn in widget-local coordinates: the full
        widget rect, filled exactly — no fit-to-size scaling, no centering,
        no letterbox margin (removed per SNX-21; `Editor` now sizes this
        widget to the frame's own logical size, so the image always fills
        it exactly). Still degenerates to an empty rect for a zero-size
        image, the one case "just fill the widget" can't sensibly mean
        anything — widget_to_image/image_to_widget both lean on that to
        keep dividing by self.image's dimensions safe. Recomputed from
        current widget size on every call — nothing cached to invalidate on
        resize.
        """
        image_size = self.image.size()
        if image_size.width() <= 0 or image_size.height() <= 0:
            return QRectF()  # degenerate image: nothing to draw, no target
        return QRectF(self.rect())

    def widget_to_image(self, point: QPointF) -> QPointF | None:
        """Widget-local point -> image-pixel point, or None if point falls
        outside the widget (and therefore outside the drawn image, which
        now always fills the widget exactly).

        Uses independent x/y scale factors, not one shared scalar, mirroring
        Frame.crop()'s own reasoning in capture.py — nothing guarantees
        _target_rect's aspect ratio matches self.image's exactly once
        rounding is involved, so trusting a single axis's ratio for both
        would be wrong on the other axis.
        """
        target = self._target_rect()
        if target.width() <= 0 or target.height() <= 0 or not target.contains(point):
            return None
        scale_x = target.width() / self.image.width()
        scale_y = target.height() / self.image.height()
        local = point - target.topLeft()
        return QPointF(local.x() / scale_x, local.y() / scale_y)

    def image_to_widget(self, point: QPointF) -> QPointF:
        """Image-pixel point -> widget-local point. Total (no None case):
        callers hold image-space points that came from inside the image by
        construction, unlike widget-space points which can land anywhere.

        Guards the same degenerate-image case widget_to_image guards via
        _target_rect()'s 0x0 fallback, so this can't divide by zero the way
        an unguarded version would (self.image.width()/height() == 0 and
        target.width()/height() == 0.0 together, if this weren't checked
        first). There's no meaningful scale to invert for an empty image, so
        this returns target.topLeft() (itself (0, 0) in that case) rather
        than raising — degrading the same way widget_to_image degrades to
        None, just without a None case to return through.
        """
        target = self._target_rect()
        if (
            target.width() <= 0
            or target.height() <= 0
            or self.image.width() <= 0
            or self.image.height() <= 0
        ):
            return target.topLeft()
        scale_x = target.width() / self.image.width()
        scale_y = target.height() / self.image.height()
        return target.topLeft() + QPointF(point.x() * scale_x, point.y() * scale_y)

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

    def _shape_index_at(self, image_point: QPointF) -> int | None:
        """The list index of the topmost shape a click lands on, or None.

        Walks _shapes back to front so an overlap resolves to whichever
        shape was drawn most recently — the one actually visible at that
        pixel, per render()'s draw-order-is-paint-order contract. Returns an
        index (not the shape itself) so the caller can remove the exact
        instance hit even if an identical-valued shape appears earlier in
        the list — dataclass equality would otherwise make list.remove()
        ambiguous between them.
        """
        for index in range(len(self._shapes) - 1, -1, -1):
            if _eraser_hit_path(self._shapes[index]).contains(image_point):
                return index
        return None

    def mousePressEvent(self, event) -> None:
        if event.button() != Qt.MouseButton.LeftButton or self._tool is None:
            return
        image_point = self.widget_to_image(event.position())
        if image_point is None:
            return  # press landed in the letterbox margin: a no-op, like Overlay

        if self._tool is Tool.ERASER:
            index = self._shape_index_at(image_point)
            if index is not None:
                del self._shapes[index]
                self._push_history()
                self.update()
            return  # erases on press, like STEP_MARKER; never arms a drag

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

    Frameless and always-on-top, like `Overlay` in overlay.py, and — per
    SNX-21 — placed so `canvas` sits exactly over the screen region the
    snip was captured from, at 1:1: without that, the window landed
    wherever the window manager felt like putting an ordinary titled
    window, sized by layout rather than by the snip, and `Canvas` scaled
    the image down to fit whatever space that left, leaving dead margins
    where a click was silently a no-op. See `_position_over_snip`.

    Per SNX-26, the tool row is two tiers rather than eleven equal-weight
    actions in one line: `PRIMARY_TOOLS` sits directly on the toolbar, and
    everything else hangs off the "More Tools" button built alongside it in
    `_build_tool_actions`. No tool is removed or made harder to use than a
    second click — the split is presentation, not capability.

    Per SNX-28, `canvas` being pixel-identical to the live desktop beneath
    it (same pixels, no frame, no shadow) made the edit surface invisible on
    a real session — a user could not tell where the snip ended and the
    live desktop began. This window now spans the whole virtual desktop
    (`desktop_frame`'s geometry, not just the snip's) rather than being
    sized tightly around toolbar+canvas, so there is real screen area
    outside the snip for `paintEvent` to dim — the same veil `Overlay`
    paints outside the selection rect during selection, reused here so
    selecting and editing look consistent rather than introducing a new
    idiom. `canvas` and `toolbar` are positioned as plain children (not a
    layout) at their exact spot within that larger window, and paint over
    the dimmed background completely, so the veil is never actually visible
    through them.
    """

    # A small one-click preset row, kept alongside the full picker added by
    # SNX-25 (see _build_colour_picker_action) rather than replaced by it —
    # a modal is the right tool for an arbitrary colour, but a needless
    # detour for the common case of "just pick a preset." The exact palette
    # is an implementation detail, not an acceptance criterion.
    SWATCH_COLOURS = [
        QColor(Qt.GlobalColor.black),
        QColor(Qt.GlobalColor.red),
        QColor(Qt.GlobalColor.green),
        QColor(Qt.GlobalColor.blue),
        QColor(Qt.GlobalColor.yellow),
    ]

    # The tools that were actually used once the toolbar overflowed on a
    # real session (per SNX-26) plus pen, the default tool -- everything
    # else moves into the "More Tools" menu instead of the main row. Order
    # here is the order they appear in that row.
    PRIMARY_TOOLS = (Tool.PEN, Tool.HIGHLIGHTER, Tool.ERASER, Tool.CROP)

    MIN_STROKE_WIDTH = 1
    MAX_STROKE_WIDTH = 20

    # Same alpha/colour Overlay.VEIL_COLOR uses in overlay.py (not imported
    # from there -- reusing the RGBA value is what keeps selection and
    # editing looking consistent, not sharing the symbol). Painted outside
    # `_snip_local_rect` in `paintEvent`.
    VEIL_COLOR = QColor(0, 0, 0, 120)
    # White reads clearly against both a bright and a dark capture sitting
    # right at the snip's own edge, unlike a fixed hue (e.g. Overlay's red
    # crosshair) which can vanish against a same-hued screenshot.
    BORDER_COLOR = QColor(255, 255, 255)
    BORDER_WIDTH = 2

    def __init__(self, frame: Frame, desktop_frame: Frame | None = None, parent=None):
        super().__init__(parent)
        self.canvas = Canvas(frame, self)

        # None means "no wider desktop to dim" -- `frame` doubles as its own
        # desktop, so `_position_over_snip` below sizes this window tightly
        # around toolbar+canvas exactly as it did before SNX-28, with no
        # margin to paint a veil into. Every real caller (AppController)
        # passes the actual, uncropped capture; this default only matters
        # for the many existing callers (tests, mostly) that construct an
        # Editor from a snip alone.
        self._desktop_frame = desktop_frame if desktop_frame is not None else frame

        # Matches the Windows Snipping Tool workflow: the snip lands on the
        # clipboard the instant it's confirmed, before any annotation is
        # made — done here, right after Canvas is built and before any
        # toolbar-building call, so no shape can exist yet when this runs.
        copy_image_to_clipboard(frame.image)

        # Same flags Overlay uses for the same reason: an ordinary titled
        # window is placed and sized by the window manager, not by us, and
        # this window's whole point (per SNX-21) is sitting at an exact,
        # self-chosen screen position.
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
        )

        self.toolbar = QToolBar(self)
        self.tool_actions: dict[Tool, QAction] = {}
        self._build_tool_actions(self.toolbar)
        self.colour_buttons: dict[str, QToolButton] = {}
        self._build_colour_swatches(self.toolbar)
        self._build_colour_picker_action(self.toolbar)
        self.stroke_width_spinbox = self._build_stroke_width_control(self.toolbar)
        self._build_undo_redo_clear_actions(self.toolbar)
        self._build_copy_save_done_actions(self.toolbar)

        # No layout (SNX-28 removed the QVBoxLayout this used to be): the
        # window now spans the whole desktop, not just toolbar+canvas, and a
        # layout has no way to leave the rest of that window as a dimmable
        # margin around two widgets it's meant to fill exactly. toolbar and
        # canvas are positioned as plain children instead, in
        # `_position_over_snip` below.

        # Start from a usable state (a tool armed, a colour and width set)
        # rather than requiring a click before the first drag does anything.
        default_tool = next(iter(Tool))
        self.tool_actions[default_tool].setChecked(True)
        self.canvas.set_tool(default_tool)
        self.canvas.set_colour(Canvas.DEFAULT_COLOUR)
        self.canvas.set_stroke_width(self.stroke_width_spinbox.value())

        self._position_over_snip(frame, self._desktop_frame)

    def _position_over_snip(self, frame: Frame, desktop_frame: Frame) -> None:
        """Size and place this window so `canvas` ends up covering exactly
        `frame.logical_origin`/`frame.logical_size` on screen at 1:1 (per
        SNX-21), while the window itself spans the whole of
        `desktop_frame.logical_origin`/`logical_size` (per SNX-28) so
        `paintEvent` has real desktop area outside the snip to dim.

        `canvas` is fixed to the frame's own logical size so it draws the
        image at 1:1 with no scaling and no letterbox margin (see
        `Canvas._target_rect`). The toolbar sits *above* that region rather
        than inside it — a toolbar docked over the snipped rect would cover
        part of the very image it's editing, trading one dead-click margin
        for another — so this window's own top edge is pushed up by the
        toolbar's height above `desktop_frame`'s top, same reasoning SNX-21
        applied to the snip's own top before the window covered the whole
        desktop.

        `self._snip_local_rect`/`self._desktop_local_rect` (window-local
        logical coordinates) are stashed for `paintEvent` to paint the veil
        and border against, computed once here rather than re-derived from
        widget geometry on every paint — mirrors why `Canvas._target_rect`
        is the only geometry Canvas ever needs, just cached instead of
        recomputed, since (unlike Canvas) this window's size is fixed once
        and never resized afterward.
        """
        self.canvas.setFixedSize(
            round(frame.logical_size.width()), round(frame.logical_size.height())
        )
        toolbar_height = self.toolbar.sizeHint().height()

        window_left = round(desktop_frame.logical_origin.x())
        window_top = round(desktop_frame.logical_origin.y()) - toolbar_height
        self.setGeometry(
            window_left,
            window_top,
            round(desktop_frame.logical_size.width()),
            round(desktop_frame.logical_size.height()) + toolbar_height,
        )

        self._snip_local_rect = QRectF(
            round(frame.logical_origin.x()) - window_left,
            round(frame.logical_origin.y()) - window_top,
            round(frame.logical_size.width()),
            round(frame.logical_size.height()),
        )
        self._desktop_local_rect = QRectF(
            round(desktop_frame.logical_origin.x()) - window_left,
            toolbar_height,
            round(desktop_frame.logical_size.width()),
            round(desktop_frame.logical_size.height()),
        )

        # Canvas sits exactly on the snip; the toolbar matches the snip's
        # own width (not the whole, possibly much wider, window) and sits
        # directly above it -- same relationship SNX-21 established, just
        # positioned as plain children now instead of via a layout that
        # would otherwise stretch to fill this larger window.
        snip_left = round(self._snip_local_rect.left())
        snip_top = round(self._snip_local_rect.top())
        self.canvas.move(snip_left, snip_top)
        self.toolbar.setFixedWidth(round(self._snip_local_rect.width()))
        self.toolbar.move(snip_left, snip_top - toolbar_height)
        self.toolbar.resize(self.toolbar.sizeHint().width(), toolbar_height)

    def paintEvent(self, event) -> None:
        """Paints the desktop this window spans, dimmed everywhere outside
        the snip, with a border marking the snip's edge -- per SNX-28.

        `canvas`/`toolbar` are ordinary child widgets, painted after this
        (Qt always paints children on top of their parent), so they cover
        whatever this draws underneath them completely; the veil and border
        are never actually visible through either one, and neither is ever
        part of `canvas.image` or `_rendered_image()`'s output, so they
        never leak into a copy or save.
        """
        painter = QPainter(self)
        # The base fill covers any window area outside `desktop_frame`
        # itself (the toolbar-height strip its top edge is pushed up by,
        # per `_position_over_snip`) -- there are no real desktop pixels to
        # show there, so it just joins the dimmed veil painted next.
        painter.fillRect(self.rect(), Qt.GlobalColor.black)
        painter.drawImage(self._desktop_local_rect, self._desktop_frame.image)
        self._paint_veil(painter)
        self._paint_border(painter)
        painter.end()  # never left open across a pixmap read, per CLAUDE.md

    def _paint_veil(self, painter: QPainter) -> None:
        """Dims everywhere outside `_snip_local_rect` in one even-odd fill —
        same technique `Overlay._paint_veil` uses in overlay.py for the same
        reason: one call can't disagree with itself at the hole's edge the
        way a separate "dim, then punch a hole" pass could.
        """
        widget_rect = QRectF(self.rect())
        path = QPainterPath()
        path.addRect(widget_rect)
        snip_rect = self._snip_local_rect.intersected(widget_rect)
        if not snip_rect.isEmpty():
            path.addRect(snip_rect)
        path.setFillRule(Qt.FillRule.OddEvenFill)
        painter.fillPath(path, self.VEIL_COLOR)

    def _paint_border(self, painter: QPainter) -> None:
        """Outlines the snip's edge so it reads as a distinct surface from
        the dimmed desktop around it, not just a slightly-brighter patch."""
        pen = QPen(self.BORDER_COLOR, self.BORDER_WIDTH)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(self._snip_local_rect)

    def _build_tool_actions(self, toolbar: QToolBar) -> None:
        """Populate `self.tool_actions` with one QAction per `Tool` member —
        every tool remains selectable and every action lands in the same
        exclusive `QActionGroup`, whether it's placed directly on the
        toolbar or inside the "More Tools" menu below. Qt doesn't care
        which widget an action's `QAction` is displayed in for `.trigger()`
        or the group's checked-state bookkeeping to work, so callers (and
        tests) can keep addressing tools via `self.tool_actions[tool]`
        exactly as before.
        """
        group = QActionGroup(toolbar)
        group.setExclusive(True)

        def build_action(tool: Tool) -> QAction:
            action = QAction(_tool_label(tool), toolbar)
            action.setCheckable(True)
            # Default arg binds `tool` at definition time, not call time —
            # without it every action's handler would close over whichever
            # `tool` the loop last landed on.
            action.triggered.connect(
                lambda checked, tool=tool: self.canvas.set_tool(tool)
            )
            group.addAction(action)
            self.tool_actions[tool] = action
            return action

        for tool in self.PRIMARY_TOOLS:
            toolbar.addAction(build_action(tool))

        # Everything else stays reachable, just not in the main row: per
        # SNX-26, eleven equal-weight actions plus the swatches and spin box
        # overflowed into QToolBar's own chevron on a real capture.
        self.more_tools_menu = QMenu("More Tools", toolbar)
        for tool in Tool:
            if tool in self.PRIMARY_TOOLS:
                continue
            self.more_tools_menu.addAction(build_action(tool))

        self.more_tools_button = QToolButton(toolbar)
        self.more_tools_button.setText("More Tools")
        self.more_tools_button.setToolTip("More Tools")
        self.more_tools_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.more_tools_button.setMenu(self.more_tools_menu)
        toolbar.addWidget(self.more_tools_button)

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

    def _build_colour_picker_action(self, toolbar: QToolBar) -> None:
        """A full colour picker for anything the preset swatches don't
        cover, per SNX-25.

        The QColorDialog itself is only ever constructed inside
        `_pick_colour`, which nothing calls except this action's `triggered`
        signal — so building the toolbar (including in every test that
        merely constructs an Editor) can never pop a modal.
        """
        self.colour_picker_action = QAction("Custom Colour…", toolbar)
        self.colour_picker_action.triggered.connect(self._pick_colour)
        toolbar.addAction(self.colour_picker_action)

    def _pick_colour(self) -> None:
        # Seeded with the current colour so re-opening the dialog starts
        # from where annotation is now, not from some fixed default.
        # QColorDialog.getColor() returns an invalid QColor on Cancel
        # (rather than raising or returning None), so isValid() is the
        # correct "did the user actually choose something" check here.
        colour = QColorDialog.getColor(self.canvas.colour, self, "Custom Colour")
        if colour.isValid():
            self.canvas.set_colour(colour)

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

    def _build_undo_redo_clear_actions(self, toolbar: QToolBar) -> None:
        """Undo/Redo/Clear controls, per SNX-22: Canvas.undo()/redo() and
        their Ctrl+Z/Ctrl+Shift+Z bindings already existed, but nothing in
        the toolbar surfaced them — a user had no way to discover them short
        of guessing the shortcut. Undo/Redo's enabled state is kept live via
        Canvas.history_changed rather than computed once at build time, so a
        user watching the toolbar sees exactly what Canvas.can_undo/
        can_redo see, instead of a second copy of that logic drifting out of
        sync with it.
        """
        toolbar.addSeparator()

        self.undo_action = QAction("Undo", toolbar)
        self.undo_action.triggered.connect(self.undo)
        toolbar.addAction(self.undo_action)

        self.redo_action = QAction("Redo", toolbar)
        self.redo_action.triggered.connect(self.redo)
        toolbar.addAction(self.redo_action)

        self.clear_action = QAction("Clear", toolbar)
        self.clear_action.triggered.connect(self.canvas.clear)
        toolbar.addAction(self.clear_action)

        self.canvas.history_changed.connect(self._update_undo_redo_actions)
        self._update_undo_redo_actions()  # starting state: both disabled

    def _update_undo_redo_actions(self) -> None:
        self.undo_action.setEnabled(self.canvas.can_undo)
        self.redo_action.setEnabled(self.canvas.can_redo)

    def _build_copy_save_done_actions(self, toolbar: QToolBar) -> None:
        """Copy/Save/Done controls, per SNX-24: `_save()` already existed
        but was reachable only via Ctrl+S, and the auto-copy-on-open (see
        `copy_image_to_clipboard` call above) fires once, before any
        annotation exists — there was no toolbar-visible way to copy or
        save the annotated result, or to finish beyond the window's own
        (frameless, so effectively invisible) close affordance. Both Copy
        and Save call through `_rendered_image()`, the same render() call
        Ctrl+S already used, so every path acts on the same annotated
        image; Done simply closes the window, same as Escape already does.
        """
        toolbar.addSeparator()

        self.copy_action = QAction("Copy", toolbar)
        self.copy_action.triggered.connect(self._copy)
        toolbar.addAction(self.copy_action)

        self.save_action = QAction("Save", toolbar)
        self.save_action.triggered.connect(self._save)
        toolbar.addAction(self.save_action)

        self.done_action = QAction("Done", toolbar)
        self.done_action.triggered.connect(self.close)
        toolbar.addAction(self.done_action)

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

    def _rendered_image(self):
        """The currently-rendered, annotated image — not the raw capture.

        Built from Canvas's public `shapes` property (a confirmed-shapes
        copy), not `Canvas._visible_shapes()`, which also includes an
        in-progress drag: that's the paint-preview's concern, not the
        save/copy contract's. Shared by `_save()` and `_copy()` (SNX-24) so
        both act on exactly the same image, per the ticket's acceptance
        criteria.
        """
        return render(self.canvas.image, list(self.canvas.shapes))

    def _save(self) -> None:
        """Save the currently-rendered, annotated image to the default
        location, without prompting for a name."""
        save_image(self._rendered_image())

    def _copy(self) -> None:
        """Place the currently-rendered, annotated image on the clipboard —
        unlike the auto-copy in __init__, which runs once before any
        annotation exists, this is callable any time after drawing to
        re-copy the up-to-date result."""
        copy_image_to_clipboard(self._rendered_image())

    def keyPressEvent(self, event) -> None:
        # Mirrors Overlay's own Escape handling in overlay.py: closes
        # without calling _save(), same as cancelling a selection never
        # emits `confirmed`. Checked first since it needs no modifier
        # comparison and should win regardless of what else this method
        # grows to handle.
        if event.key() == Qt.Key.Key_Escape:
            self.close()
            return

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
