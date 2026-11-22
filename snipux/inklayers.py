"""The overlay's ink caches: the images a repaint composes from the frozen
frame and the committed marks, and each mark's bounds.

A repaint happens on every mouse move of a drag and every tick of the
marching ants, and none of what is held here changes at that rate: marks
change only through the `MarkStore`, and the frame only on a delayed
re-capture. `InkLayers` builds each image when what it was built from
changes and hands the same one back until then.

Separate from `overlay.py` because it is the one piece of the overlay's
painting with state of its own and no need of the window: it is handed the
frame, the marks and the sizes to build for, and returns images. That
keeps every cache and the rule for when it is stale in one place, away
from the code that decides what to paint. Nothing in this module imports
`overlay.py`.

Coordinates: marks are in the caller's window-local logical pixels
throughout. The base and covered images are in the frame's own pixels;
the ink layer is window-sized, at the device pixel ratio it is given.
"""

from __future__ import annotations

import math
from dataclasses import replace

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import QImage, QPainter

from snipux import design
from snipux.capture import Frame
from snipux.shapes import (
    Highlighter,
    ObscuringShape,
    Shape,
    Spotlight,
    StepMarker,
    Text,
    render_selection,
)


def covered_mark_count(marks: "tuple[Shape, ...] | list[Shape]") -> int:
    """How many leading marks have to be flattened together because an
    obscuring mark was drawn over ink: everything up to and including
    the last obscuring mark, or 0 when no ink comes before one.

    0 is the ordinary case, and leaves painting exactly as it was:
    obscuring marks baked into Layer 1, ink drawn live over them.
    """
    last = max(
        (index for index, shape in enumerate(marks) if isinstance(shape, ObscuringShape)),
        default=-1,
    )
    if any(not isinstance(shape, ObscuringShape) for shape in marks[:last]):
        return last + 1
    return 0


class InkLayers:
    """One overlay window's cached layers. See the module docstring."""

    def __init__(self) -> None:
        # SNX-63: cached output of `base_layer_image` -- `(key, image)`,
        # or None before the first paint -- so a repaint triggered by
        # something unrelated to any committed `ObscuringShape` (an
        # in-progress pen stroke elsewhere, a resize drag, marching ants)
        # does not redo blur/pixelate's scale-down-then-up sampling every
        # single frame. See that method's own docstring for what `key`
        # covers and why equality against it is enough to know the cached
        # image is still correct.
        self._base_layer_cache: tuple[tuple, QImage] | None = None
        # Cached output of `covered_ink_image`, with what it was built
        # from: the frame, the marks themselves and each obscuring mark's
        # geometry. Holding the marks rather than their ids is what makes
        # an identity check safe after one of them has been freed.
        self._covered_ink_cache: "tuple[Frame, tuple[Shape, ...], tuple, QImage] | None" = None
        # id(mark) -> its bounds; dropped whenever the marks change.
        self._mark_bounds_cache: "dict[int, QRectF | None] | None" = None
        # (key, the marks drawn on it, the image) -- see `ink_layer_image`.
        self._ink_layer_cache: "tuple[tuple, tuple[Shape, ...], QImage] | None" = None

    def marks_changed(self) -> None:
        """The marks are no longer the ones the bounds were worked out
        for. The images check for themselves; see each method."""
        self._mark_bounds_cache = None

    def release_ink_layer(self) -> None:
        """Let go of the ink layer: a window-sized image, not worth
        holding while nothing shows it."""
        self._ink_layer_cache = None

    def base_layer_image(
        self, frame: Frame, marks: "tuple[Shape, ...] | list[Shape]", scale: tuple[float, float]
    ) -> QImage:
        """Layer 1's own source pixels: `frame.image` with every
        committed `ObscuringShape` mark's blur/pixelate effect actually
        baked in, in list order -- each one sampling the output of every
        obscuring mark before it, the same left-to-right composition
        `shapes.render()` uses for export, via repeated `apply()` calls
        rather than `render()` itself (which would also flatten every
        *non*-obscuring mark permanently into the image, losing the
        live, undo-able painter drawing `OverlayWindow._paint_marks` still
        wants for those).

        Fixes SNX-63: `_paint_marks` used to skip every `ObscuringShape`
        outright with a "later ticket" comment that no later ticket ever
        picked up, so a committed blur/pixelate was invisible until
        export. Baking it into Layer 1 here instead of painting it inside
        `_paint_marks` is what keeps it aligned across a re-frame for
        free -- this method never reads the selection at all, only
        `marks` and `frame`, so the obscured patch sits over exactly
        the pixels it was drawn on regardless of where the selection
        rect (and its clip) currently is.

        Cached against `key` -- each obscuring mark's class, geometry and
        `strength`, plus the frame's own identity -- and only recomputed
        when that key actually changes, per this ticket's "does not
        visibly stall" acceptance criterion: a repaint triggered by
        something unrelated (an in-progress stroke elsewhere, marching
        ants, a resize drag) must not redo the scale-down/scale-up
        sampling for every obscuring mark on every single frame. Keying
        on `strength` rather than just object identity is also what
        makes an already-committed mark's on-screen look track a change
        to its own `strength` (e.g. a direct write to the mark) on the
        very next repaint, instead of it staying stuck at whatever this
        method last cached.

        A mark's `start`/`end` are in the window's own window-local
        coordinates and need converting into `frame.image`'s own,
        larger-under-HiDPI pixel space before `apply()` -- which expects
        both its rect and the image it samples to share one coordinate
        space -- can sample the right pixels. `scale` is that conversion,
        frame pixels per window-local logical pixel on each axis; see
        `OverlayWindow._window_to_frame_scale`.

        Every `Spotlight` among `obscuring` is combined into one
        `Spotlight.apply_all()` call instead of taking its own turn in the
        loop -- shapes.render()'s own docstring gives the reasoning, which
        applies here unchanged: several holes in one dim, not one dim per
        spotlight redimming the last one's.
        """
        obscuring = [shape for shape in marks if isinstance(shape, ObscuringShape)]
        key = (
            id(frame),
            tuple(
                (
                    type(shape).__name__,
                    shape.start.x(),
                    shape.start.y(),
                    shape.end.x(),
                    shape.end.y(),
                    shape.strength,
                )
                for shape in obscuring
            ),
        )
        if self._base_layer_cache is not None and self._base_layer_cache[0] == key:
            return self._base_layer_cache[1]

        image = frame.image
        if obscuring:
            scale_x, scale_y = scale
            scaled = [
                replace(
                    shape,
                    start=QPointF(shape.start.x() * scale_x, shape.start.y() * scale_y),
                    end=QPointF(shape.end.x() * scale_x, shape.end.y() * scale_y),
                )
                for shape in obscuring
            ]
            # Spotlight is combined rather than applied one at a time, the
            # same special case shapes.render() makes for export -- see
            # Spotlight's own docstring for why stacking each one's apply()
            # would leave only their rects' intersection lit.
            spotlights = [shape for shape in scaled if isinstance(shape, Spotlight)]
            spotlights_done = False
            for shape in scaled:
                if isinstance(shape, Spotlight):
                    if spotlights_done:
                        continue
                    image = Spotlight.apply_all(image, spotlights)
                    spotlights_done = True
                    continue
                image = shape.apply(image)

        self._base_layer_cache = (key, image)
        return image

    def covered_ink_image(
        self, frame: Frame, marks: "tuple[Shape, ...] | list[Shape]", count: int
    ) -> QImage:
        """The frame with the first `count` of `marks` flattened onto it in
        the order they were drawn, in the frame's own pixels.

        Marks apply in order, so a blackout or blur drawn after a pen
        stroke covers it -- that is what `shapes.render` exports. On screen
        the stroke used to show on top: every obscuring mark is baked into
        Layer 1 and all ink is painted over that, whichever came first.
        This is the export's own flattening, run over the whole frame, so
        the two cannot disagree; `OverlayWindow._paint_marks` draws it
        clipped to the selection in place of those marks.

        Cached like `base_layer_image`, and only ever built when ink sits
        under an obscuring mark.
        """
        covered = marks[:count]
        obscuring_key = tuple(
            (shape.start.x(), shape.start.y(), shape.end.x(), shape.end.y(), shape.strength)
            for shape in covered
            if isinstance(shape, ObscuringShape)
        )
        cache = self._covered_ink_cache
        if (
            cache is not None
            and cache[0] is frame
            and len(cache[1]) == count
            and all(was is now for was, now in zip(cache[1], covered))
            and cache[2] == obscuring_key
        ):
            return cache[3]
        whole_frame = QRectF(QPointF(0, 0), frame.logical_size)
        image = render_selection(frame, list(covered), whole_frame)
        self._covered_ink_cache = (frame, covered, obscuring_key, image)
        return image

    def ink_layer_image(
        self,
        marks: "tuple[Shape, ...] | list[Shape]",
        covered: int,
        width: int,
        height: int,
        ratio: float,
    ) -> QImage | None:
        """Every committed mark from `covered` on that draws itself, on a
        transparent image `width` x `height` logical pixels -- the size of
        the window -- at device pixel ratio `ratio`. None when there is
        nothing to draw.

        Stroking the marks from their vectors was most of a repaint once a
        snip had some ink on it -- 61% of the profile with 40 pen strokes,
        and a repaint happens on every mouse move of a drag and every tick
        of the marching ants. Marks only change through the `MarkStore`, so
        they are drawn once here and blitted after that.

        A mark added to the end is drawn onto the image already held, which
        is the whole cost of finishing a stroke; any other change starts the
        image again.
        """
        marks = tuple(
            shape
            for shape in marks[covered:]
            if not isinstance(shape, ObscuringShape)
        )
        if not marks:
            self._ink_layer_cache = None
            return None
        key = (covered, width, height, ratio)
        cached = self._ink_layer_cache
        drawn: tuple[Shape, ...] = ()
        image = None
        if cached is not None and cached[0] == key:
            _key, held, image = cached
            if len(held) <= len(marks) and all(
                a is b for a, b in zip(held, marks)
            ):
                drawn = held
            else:
                image = None
        if image is None:
            image = QImage(
                math.ceil(width * ratio),
                math.ceil(height * ratio),
                QImage.Format.Format_ARGB32_Premultiplied,
            )
            image.setDevicePixelRatio(ratio)
            image.fill(Qt.GlobalColor.transparent)
        if len(drawn) < len(marks):
            ink_painter = QPainter(image)
            for shape in marks[len(drawn):]:
                shape.draw(ink_painter)
            # Closed before the image is read by the caller's drawImage.
            ink_painter.end()
        self._ink_layer_cache = (key, marks, image)
        return image

    def marks_in(
        self, marks: "tuple[Shape, ...] | list[Shape]", selection: QRectF
    ) -> list[Shape]:
        """The `marks` that can show inside `selection`, both in the same
        space.
        """
        # Each mark's bounds are worked out once per change to the marks,
        # not once per paint: the size chip asks this on every repaint, and
        # walking every point of every pen stroke was most of a repaint
        # once a snip had some ink on it.
        if self._mark_bounds_cache is None:
            self._mark_bounds_cache = {id(shape): _mark_bounds(shape) for shape in marks}
        bounds = self._mark_bounds_cache
        return [
            shape
            for shape in marks
            if (reach := bounds[id(shape)]) is None or reach.intersects(selection)
        ]


def _mark_bounds(shape: Shape) -> QRectF | None:
    """Everything `shape` can paint on, in its own space, or None for a
    mark that shows everywhere.

    Taken from the mark's own points, widened by what it paints around
    them, so it errs toward too large: the question it answers is only ever
    whether ink is certainly out of sight. Dispatches on field name, as
    `shapes._transformed` does.
    """
    if isinstance(shape, Spotlight):
        # Dims everything outside its own rect.
        return None
    if isinstance(shape, Text):
        return QRectF(shape.chip_rect())
    reach = shape.stroke_width
    if hasattr(shape, "points"):
        xs = [point.x() for point in shape.points]
        ys = [point.y() for point in shape.points]
        bounds = QRectF(QPointF(min(xs), min(ys)), QPointF(max(xs), max(ys))) if xs else QRectF()
        for band in getattr(shape, "bands", None) or ():
            bounds = bounds.united(band)
        if isinstance(shape, Highlighter):
            reach *= design.tokens.Metric.HIGHLIGHT_MULT
    elif hasattr(shape, "start") and hasattr(shape, "end"):
        bounds = QRectF(shape.start, shape.end).normalized()
    else:
        bounds = QRectF(shape.point, shape.point)
        reach = StepMarker.RADIUS
    # At least a pixel: a rect with no width intersects nothing.
    reach = max(reach, 1.0)
    return bounds.adjusted(-reach, -reach, reach, reach)


def _mark_reaches(shape: Shape, selection: QRectF) -> bool:
    """Whether any of `shape` can show inside `selection`, both in the same
    space."""
    bounds = _mark_bounds(shape)
    return bounds is None or bounds.intersects(selection)
