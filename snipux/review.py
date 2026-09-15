"""The review window: what a snip looks like once the overlay has closed.

`docs/design/handoff-windows.md` section 3 is the authority. 1020 x 700, the
same `Win` chrome as Settings, and a canvas that answers the two questions
the flow could not: **what did I capture**, and **where did it go**.

Annotate mode reveals *the overlay's own floating bar* over the image -- the
same widget, the same tools, the same `MarkStore`. It is not a second
editor and must not become one; the only differences the design allows are
that there is no capture-mode chip (nothing left to capture) and the
trailing action is `Done` rather than Save (the footer already owns the
exports). The watermark slot goes with `Done`: the snip this window opens
was exported by the overlay, stamped already if the watermark was on, so its
exports stamp nothing more.

The one real divergence from the overlay is coordinates: marks live in
**image** space here, not screen space, because the image is the document.
`ImageCanvas` maps pointer positions through the current zoom, so drawing
stays correct at any magnification and an exported mark lands where it
looked.
"""

from __future__ import annotations

import os
from pathlib import Path

from PyQt6.QtCore import QEvent, QPointF, QRectF, Qt, QUrl, pyqtSignal
from PyQt6.QtGui import (
    QColor,
    QDesktopServices,
    QGuiApplication,
    QImage,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPixmap,
    QRadialGradient,
)
from PyQt6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from . import design, setup_desktop, shapes
from .design import tokens
from .marks import (
    MarkStore,
    TextLabelEditor,
    ToolStyles,
    begin_stroke,
    extend_stroke,
    session_styles,
)
from .overlay import (
    FamilyMenu,
    FloatingBar,
    StylePopover,
    Toast,
    ToolHintStrip,
)
from .winchrome import AccentButton, SecondaryButton, WinWindow, _mono_font, _ui_font


class ImageCanvas(QWidget):
    """The radial workspace, the screenshot on it, and the ink over both.

    The screenshot is drawn with a border, a soft shadow and a faint outer
    ring, because the whole point of the redesign is that **the image has an
    edge** -- on the overlay the capture bleeds into the desktop behind it
    and the user cannot tell where it stops.

    Marks are stored in image coordinates and painted through the same
    transform the image is, so zooming moves ink and pixels together.
    """

    marksChanged = pyqtSignal()

    # Screen pixels of aim the eraser forgives, converted to image units
    # against the live zoom.
    _ERASER_SLACK_PX = 7.0

    def __init__(
        self,
        image: QImage,
        store: MarkStore,
        parent: QWidget | None = None,
        *,
        styles: ToolStyles | None = None,
    ):
        super().__init__(parent)
        self._image = image
        self._store = store
        self._zoom = 100
        self._annotating = False
        self._tool: str | None = None
        # What each tool draws with: the overlay's own per-tool style, for
        # the session, unless a caller hands in another.
        self._styles = styles if styles is not None else session_styles
        self._in_progress: shapes.Shape | None = None
        # True for the duration of an eraser press, so a drag rubs out
        # everything it passes over rather than only what it started on.
        self._erasing = False
        self._composite_key: tuple | None = None
        self._composite: QImage = image
        # The same label editor the overlay uses. `to_image` is the only
        # difference: the field is placed at the widget point clicked, but
        # the mark it commits is stored in image coordinates, like every
        # other mark here.
        self._text_editor = TextLabelEditor(self, store, to_document=self.to_image)
        self.setMouseTracking(True)
        self._store.changed.connect(self.update)

    def glass_backdrop(self) -> None:
        """None: the bar, the strip and the menus over this canvas keep the
        fill they were designed with, with no blur under it and no fallback
        (`snipux.glass`).

        What is behind them here is this canvas, not a frozen frame, and it
        does not hold still: a zoom or a stroke changes it under the bar. A
        crop blurred once would be stale by the next of those, and one
        blurred for each is the live blur the handoff rules out. At the fit
        this window opens on, the bar sits on the workspace gradient, which
        a blur would leave as it is. The fallback's denser fill is for glass
        that should have had a blur of a busy desktop under it; the ground
        here is the window's own.
        """
        return None

    # -- geometry --------------------------------------------------------

    @property
    def zoom(self) -> int:
        return self._zoom

    def set_zoom(self, percent: int) -> None:
        low, high, _step = tokens.WinMetric.ZOOM_STEPS
        self._zoom = max(low, min(percent, high))
        self.update()

    def _scale(self) -> float:
        """Image pixels -> widget pixels.

        The zoom percentage applies on top of a fit-to-canvas scale, so 100%
        means "as large as this window can show it" rather than 1:1 -- a
        1377 x 936 snip in a 1020px window is otherwise clipped at every
        zoom level the design offers.
        """
        if self._image.isNull():
            return 1.0
        available_w = max(1, self.width() - 96)
        available_h = max(1, self.height() - 96)
        fit = min(available_w / self._image.width(), available_h / self._image.height(), 1.0)
        return fit * (self._zoom / 100)

    def image_rect(self) -> QRectF:
        """Where the screenshot sits in this widget, at the current zoom."""
        scale = self._scale()
        width = self._image.width() * scale
        height = self._image.height() * scale
        return QRectF(
            (self.width() - width) / 2, (self.height() - height) / 2, width, height
        )

    def to_image(self, point: QPointF) -> QPointF:
        """Widget point -> image coordinates.

        The design's rule, and the reason marks survive a zoom: scale by
        `image_width / displayed_width` rather than storing what the pointer
        happened to be over on screen.
        """
        rect = self.image_rect()
        scale = self._scale() or 1.0
        return QPointF((point.x() - rect.x()) / scale, (point.y() - rect.y()) / scale)

    # -- annotation ------------------------------------------------------

    def is_annotating(self) -> bool:
        return self._annotating

    def abandon_text(self) -> None:
        """Escape's first stage while a label is focused."""
        self._text_editor.abandon()

    def has_active_label(self) -> bool:
        return self._text_editor.is_active()

    def set_annotating(self, annotating: bool) -> None:
        if not annotating:
            self._text_editor.commit()
        self._annotating = annotating
        self.setCursor(
            Qt.CursorShape.CrossCursor if annotating else Qt.CursorShape.ArrowCursor
        )
        self.update()

    def set_tool(self, tool: str | None) -> None:
        self._tool = tool

    def rendered_image(self) -> QImage:
        """The image with every mark flattened onto it.

        Marks are already in image coordinates, so unlike the overlay's own
        export there is no translation step -- which is most of why the
        design puts them in this space.
        """
        return shapes.render(self._image, list(self._store.marks))

    def mousePressEvent(self, event: QMouseEvent) -> None:
        # A press while a drag is still open means that drag's release never
        # arrived. It ends here, as it stands, before this press starts
        # anything.
        self.end_drag()
        if not self._annotating or event.button() != Qt.MouseButton.LeftButton:
            return
        position = self.to_image(event.position())
        style = self._styles.of(self._tool)
        if self._tool == "eraser":
            # Armed for the whole press, so the eraser can be swept over a
            # group of marks instead of aimed at each one -- see
            # `mouseMoveEvent`. Rubbing something out is a sweep everywhere
            # else it exists.
            self._erasing = True
            self._erase_at(position)
            return
        if self._tool == "text":
            # Placed where the click landed, in widget coordinates -- the
            # editor converts to image coordinates for the mark itself.
            self._text_editor.begin(event.position(), QColor(style.colour), style.size)
            return
        # Any other tool ends a label still being typed, rather than
        # abandoning it: a click elsewhere never blurs the field, so nothing
        # else would force the commit.
        self._text_editor.commit()
        if self._tool == "step":
            # Click only, no drag -- the same rule the overlay applies.
            self._store.add(
                shapes.StepMarker(
                    colour=QColor(style.colour),
                    stroke_width=style.size,
                    point=position,
                    number=shapes.next_step_number(list(self._store.marks)),
                )
            )
            self.marksChanged.emit()
            return
        self._in_progress = begin_stroke(
            self._tool,
            position,
            colour=QColor(style.colour),
            stroke_width=style.size,
            blur_strength=style.strength,
            fill=style.fill,
            dash=style.dash,
        )
        self.update()

    def _erase_at(self, position: QPointF) -> None:
        """Remove whatever is under `position`, with slack for aim.

        A mark's hit tolerance is fixed in image units, so at anything under
        100% zoom it shrinks on screen to almost nothing; the slack is the
        screen-pixel allowance converted back through the live scale.
        """
        scale = self._scale() or 1.0
        if self._store.erase(position, slack=self._ERASER_SLACK_PX / scale):
            self.marksChanged.emit()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if not event.buttons() & Qt.MouseButton.LeftButton:
            # Nothing is held, so a drag still open lost its release. Ending
            # it here stops ordinary movement stretching the mark.
            self.end_drag()
            return
        if self._erasing:
            self._erase_at(self.to_image(event.position()))
            return
        if self._in_progress is None:
            return
        extend_stroke(self._in_progress, self.to_image(event.position()))
        self.update()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        self.end_drag()

    def event(self, event) -> bool:
        # Focus leaving the window takes any release with it.
        if event.type() == QEvent.Type.WindowDeactivate:
            self.end_drag()
        return super().event(event)

    def end_drag(self) -> None:
        """End the eraser sweep or the mark being drawn, whichever is open.

        On a release, and on everything that means a release was lost -- a
        move with no button held, a new press, focus leaving the window --
        the same guards the overlay keeps. The mark is committed as it
        stands now, with the end its last move gave it, never as it was when
        its press began.
        """
        self._erasing = False
        shape, self._in_progress = self._in_progress, None
        if shape is None:
            return
        # `finalize_mark` is the overlay's own commit rule -- a stroke too
        # small to be deliberate is dropped rather than committed.
        finished = shapes.finalize_mark(shape)
        if finished is not None:
            self._store.add(finished)
            self.marksChanged.emit()
        self.update()

    def _composited(self) -> QImage:
        """The image with every committed obscuring mark baked in.

        Cached against those marks' identity and geometry: a repaint
        triggered by anything else -- an in-progress stroke, a resize, a
        zoom -- must not redo the sampling every frame.
        """
        obscuring = [s for s in self._store.marks if isinstance(s, shapes.ObscuringShape)]
        key = tuple(
            (type(s).__name__, s.start.x(), s.start.y(), s.end.x(), s.end.y(),
             getattr(s, "strength", None))
            for s in obscuring
        )
        if key != self._composite_key:
            result = self._image
            for shape in obscuring:
                result = shape.apply(result)
            self._composite_key, self._composite = key, result
        return self._composite

    @staticmethod
    def _draw_pending_region(painter: QPainter, shape) -> None:
        from PyQt6.QtGui import QPen

        rect = QRectF(shape.start, shape.end).normalized()
        pen = QPen(QColor(tokens.Color.ACCENT))
        pen.setStyle(Qt.PenStyle.DashLine)
        pen.setWidth(2)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(rect)

    # -- painting --------------------------------------------------------

    def paintEvent(self, event) -> None:
        metric = tokens.WinMetric
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)

        # Workspace: a radial, centred high and left of centre per the token.
        _kind, (cx, cy), radius, stops = tokens.Gradient.WORKSPACE
        gradient = QRadialGradient(
            self.width() * cx, self.height() * cy, self.width() * radius
        )
        for position, colour in stops:
            gradient.setColorAt(position, QColor(colour))
        painter.fillRect(self.rect(), gradient)

        rect = self.image_rect()
        if rect.isEmpty():
            painter.end()
            return

        # Shadow first, then the faint ring, then the image: the ring reads
        # as a halo around the edge rather than a second border only when it
        # sits under the 1px stroke.
        shadow = QColor(0, 0, 0, 90)
        for spread in range(18, 0, -3):
            shadow.setAlpha(max(4, 90 - spread * 4))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(shadow)
            painter.drawRoundedRect(rect.adjusted(-spread, -spread + 4, spread, spread + 4), 6, 6)

        ring = QColor(255, 255, 255)
        ring.setAlphaF(0.02)
        painter.setBrush(ring)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRect(rect.adjusted(-metric.REVIEW_IMG_RING, -metric.REVIEW_IMG_RING,
                                       metric.REVIEW_IMG_RING, metric.REVIEW_IMG_RING))

        painter.drawImage(rect, self._composited())

        # Ink, through the same transform the image got. Obscuring marks are
        # deliberately absent here: Blur and Pixelate sample already-rendered
        # pixels via apply() and raise from draw(), so they are baked into
        # the base image above instead -- the same split the overlay makes
        # between `_base_layer_image` and `_paint_marks`.
        painter.save()
        painter.translate(rect.topLeft())
        painter.scale(self._scale(), self._scale())
        for shape in self._store.marks:
            if not isinstance(shape, shapes.ObscuringShape):
                shape.draw(painter)
        if self._in_progress is not None:
            if isinstance(self._in_progress, shapes.ObscuringShape):
                # An in-progress obscuring mark cannot be previewed by
                # sampling on every mouse-move without stalling, so its
                # region is outlined until it commits -- better than the
                # nothing-at-all that read as the tool being broken.
                self._draw_pending_region(painter, self._in_progress)
            else:
                self._in_progress.draw(painter)
        painter.restore()

        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QColor(tokens.Win.IMAGE_BORDER))
        painter.drawRect(rect)
        painter.end()


class _Badge(QLabel):
    """The dimension and zoom clusters that float over the canvas.

    Above the image in z-order, so they are never occluded by it however
    large the snip is.
    """

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setFont(_mono_font(11.5))
        self.setStyleSheet(
            "background: rgba(18, 20, 24, 0.88);"
            f" border: 1px solid {tokens.Win.SEGMENT_BORDER};"
            " border-radius: 8px; padding: 5px 9px;"
            f" color: {tokens.Win.TEXT_SECONDARY};"
        )


class _ZoomStep(QLabel):
    """One end of the zoom cluster -- the "−" or the "+" -- as a real hit
    target.

    A QLabel rather than a QPushButton so the cluster keeps the badge's
    flat look with no per-button chrome to override; the interactivity it
    was missing is the `clicked` signal and the pointing-hand cursor, not
    a frame.
    """

    clicked = pyqtSignal()

    def __init__(self, glyph: str, parent: QWidget | None = None):
        super().__init__(glyph, parent)
        self.setFont(_mono_font(11.5))
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._enabled_look = True
        self._restyle()

    def set_enabled_look(self, enabled: bool) -> None:
        """Dim at the end of the range, so a click that cannot do anything
        does not look like one that can."""
        if enabled == self._enabled_look:
            return
        self._enabled_look = enabled
        self._restyle()

    def _restyle(self) -> None:
        colour = tokens.Win.TEXT_SECONDARY if self._enabled_look else tokens.Win.TEXT_FAINT
        self.setStyleSheet(f"background: transparent; border: none; color: {colour};")

    def mousePressEvent(self, event) -> None:
        # Emitted even when dimmed: the canvas clamps, and swallowing the
        # click here would mean two places deciding what the limits are.
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
            return
        super().mousePressEvent(event)


class _ZoomBadge(QWidget):
    """The canvas's top-right zoom cluster: minus / percentage / plus,
    `WinMetric.ZOOM_STEPS` (60-160% in steps of 20).

    This was a plain `_Badge` -- the "−" and "+" were characters inside one
    QLabel's text with nothing behind them. `ImageCanvas.set_zoom()` was
    fully implemented, clamping to the same range and rescaling the marks
    along with the image, and *nothing in the app ever called it*: the
    control the handoff specifies as interactive read "100%" and did
    nothing at any point in the window's life.

    Three children rather than one label hit-tested by x: the glyphs are
    the hit targets, so they may as well *be* the widgets, and a mono
    font's advance width stops being load-bearing for whether a click
    lands.
    """

    zoomChanged = pyqtSignal(int)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        low, high, step = tokens.WinMetric.ZOOM_STEPS
        self._low, self._high, self._step = low, high, step
        self._zoom = 100

        self.setStyleSheet(
            "background: rgba(18, 20, 24, 0.88);"
            f" border: 1px solid {tokens.Win.SEGMENT_BORDER};"
            " border-radius: 8px;"
        )

        self._minus = _ZoomStep("−", self)
        self._percent = QLabel(self)
        self._percent.setFont(_mono_font(11.5))
        self._percent.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._percent.setStyleSheet(
            f"background: transparent; border: none; color: {tokens.Win.TEXT_SECONDARY};"
        )
        self._plus = _ZoomStep("+", self)

        row = QHBoxLayout(self)
        row.setContentsMargins(9, 5, 9, 5)
        row.setSpacing(9)
        row.addWidget(self._minus)
        row.addWidget(self._percent)
        row.addWidget(self._plus)

        self._minus.clicked.connect(lambda: self._nudge(-self._step))
        self._plus.clicked.connect(lambda: self._nudge(self._step))
        self._sync()

    @property
    def zoom(self) -> int:
        return self._zoom

    def set_zoom(self, percent: int) -> None:
        """Adopt `percent` without emitting -- for following the canvas,
        which is the authority on what the zoom actually clamped to."""
        self._zoom = max(self._low, min(percent, self._high))
        self._sync()

    def _nudge(self, delta: int) -> None:
        target = max(self._low, min(self._zoom + delta, self._high))
        if target == self._zoom:
            return
        self._zoom = target
        self._sync()
        self.zoomChanged.emit(target)

    def _sync(self) -> None:
        self._percent.setText(f"{self._zoom}%")
        self._minus.set_enabled_look(self._zoom > self._low)
        self._plus.set_enabled_look(self._zoom < self._high)
        self.adjustSize()


class ReviewWindow(WinWindow):
    """The window a finished snip opens in.

    `saved_path` is where it already went, when it was saved rather than
    copied. Its absence is why `Show in Folder` starts disabled rather than
    hidden -- the window should not change shape depending on how the snip
    arrived.
    """

    def __init__(
        self,
        image: QImage,
        *,
        saved_path: Path | None = None,
        parent: QWidget | None = None,
    ):
        super().__init__(
            "Snipux",
            size=(tokens.WinMetric.REVIEW_W, tokens.WinMetric.REVIEW_H),
            parent=parent,
        )
        self._image = image
        self._saved_path = saved_path
        self._dirty = False
        self._store = MarkStore(self)

        self.title_label.setText(
            saved_path.name if saved_path is not None else "Unsaved snip"
        )
        self.title_detail.setText(f"{image.width()} × {image.height()}")

        body = QVBoxLayout(self.body)
        body.setContentsMargins(0, 0, 0, 0)

        self._styles = session_styles
        self._canvas = ImageCanvas(image, self._store, styles=self._styles)
        self._canvas.marksChanged.connect(self._on_edited)
        body.addWidget(self._canvas)

        self._dimension_badge = _Badge(self._canvas)
        self._zoom_badge = _ZoomBadge(self._canvas)
        self._zoom_badge.zoomChanged.connect(self._on_zoom_changed)
        self._refresh_badges()

        self._bar = FloatingBar(self._canvas, capture_chip=False, trailing="done")
        self._bar.hide()
        self._bar.toolSelected.connect(self._on_tool_selected)
        # Bound, not a lambda: a lambda holding this window, kept by a bar
        # this window owns, is a cycle Python cannot see into, and the
        # window -- frozen frame and all -- would never be freed.
        self._bar.toolPicked.connect(self._on_tool_picked)
        self._bar.familyMenuRequested.connect(self._toggle_family_menu)
        self._bar.styleRequested.connect(self._toggle_style)
        self._bar.undoRequested.connect(self._store.undo)
        self._bar.redoRequested.connect(self._store.redo)
        self._bar.clearRequested.connect(self._store.clear)
        self._bar.copyRequested.connect(self.copy)
        self._bar.saveRequested.connect(lambda: self._set_annotating(False))
        # Hovering a tool names it. Not Qt's tooltip: that depends on a
        # wake-up timer this bar's buttons were not feeding, and on a
        # translucent frameless parent it is unreliable anyway. The strip is
        # already on screen and already says exactly this.
        self._bar.toolHovered.connect(self._preview_tool)
        self._bar.toolUnhovered.connect(self._sync_tool_hint)

        # The overlay's own style popover, over the same per-tool style,
        # instantiated here rather than reimplemented: without it the bar's
        # tools were selectable but unconfigurable -- no pen size, no brush
        # size, no colour. The style dot opens it, and its keys work while
        # editing whether it is open or not.
        self._style_popover = StylePopover(self._styles, self._canvas)
        self._style_popover.hide()
        self._style_popover.styleChanged.connect(self._on_style_changed)
        self._style_popover.openChanged.connect(self._on_style_open_changed)

        # Names the active tool, and what it does, while the style popover is
        # closed, so there is never an active tool the user cannot identify.
        self._tool_hint = ToolHintStrip(self._canvas)
        self._tool_hint.hide()

        # The same toast the overlay uses. Copy previously changed one word
        # in the footer, which is not where anyone is looking when they
        # press the button they just pressed.
        self._toast = Toast(self._canvas)
        self._toast.hide()

        # The notched slots' menus, as the overlay has them.
        self._family_menus = {
            family: FamilyMenu(family, self._canvas) for family in tokens.FAMILIES
        }
        for menu in self._family_menus.values():
            menu.hide()
            menu.siblingPicked.connect(self._on_family_sibling_picked)
        # A press on the image while a menu is open closes the menu and does
        # nothing else, as on the overlay. The canvas knows nothing of the
        # bar's menus, so this window watches its presses for it.
        self._canvas.installEventFilter(self)

        self._store.changed.connect(self._on_edited)

        self._build_footer_contents()
        self._refresh_status()

    def _on_tool_selected(self, tool: str) -> None:
        self._canvas.set_tool(tool)
        for menu in self._family_menus.values():
            menu.hide()
        self._follow_tool_with_style(tool)
        self._sync_tool_hint()

    def _follow_tool_with_style(self, tool: str) -> None:
        """Point the style dot and the popover at `tool` -- the overlay's
        `_follow_tool_with_style`. An open popover follows a tool changed by
        key and closes for one with nothing to style.
        """
        self._style_popover.set_tool(tool)
        self._bar.set_style_preview(self._styles.of(tool))
        if self._style_popover.isHidden():
            return
        if tokens.STYLE_SECTIONS.get(tool):
            self._place_style_popover()
        else:
            self._style_popover.hide()

    def _on_style_changed(self, tool: str) -> None:
        if tool == self._bar.active_tool:
            self._bar.set_style_preview(self._styles.of(tool))

    def _on_style_open_changed(self, open_: bool) -> None:
        self._bar.set_style_open(open_)
        self._sync_tool_hint()

    def _toggle_family_menu(self, family: str) -> None:
        menu = self._family_menus[family]
        # Hidden, not visible: a window not yet on screen has no visible
        # children, and its menu would reopen on every toggle.
        if not menu.isHidden():
            menu.hide()
            return
        self._close_bar_menus()
        menu.set_current(self._bar.family_choice(family))
        menu.reposition(
            self._bar.slot_rect(family, self._canvas),
            self._bar.geometry(),
            QRectF(self._canvas.rect()),
        )
        menu.show()
        menu.raise_()

    def _on_family_sibling_picked(self, tool: str) -> None:
        self._close_bar_menus()
        self._bar.select_tool(tool)

    def _on_tool_picked(self, _tool: str) -> None:
        """A tool clicked on the bar closes whatever menu is open."""
        self._close_bar_menus()

    def _close_bar_menus(self) -> bool:
        """Close the family menus and the style popover, and say whether any
        of them was open.
        """
        was_open = not self._style_popover.isHidden() or any(
            not menu.isHidden() for menu in self._family_menus.values()
        )
        for menu in self._family_menus.values():
            menu.hide()
        self._style_popover.hide()
        return was_open

    def _toggle_style(self) -> None:
        """The style dot: open the popover for the active tool, or close it."""
        if not self._style_popover.isHidden():
            self._style_popover.hide()
            return
        tool = self._bar.active_tool
        if not self._canvas.is_annotating() or not tokens.STYLE_SECTIONS.get(tool):
            return
        self._close_bar_menus()
        self._style_popover.set_tool(tool)
        self._place_style_popover()
        self._style_popover.show()
        self._style_popover.raise_()

    def _place_style_popover(self) -> None:
        self._style_popover.reposition(
            self._bar.style_dot_rect(self._canvas),
            self._bar.geometry(),
            QRectF(self._canvas.rect()),
        )

    def eventFilter(self, watched, event) -> bool:
        # The type first: the window's own chrome installs filters before
        # this window has a canvas to compare against.
        if (
            event.type() == QEvent.Type.MouseButtonPress
            and watched is getattr(self, "_canvas", None)
            and self._close_bar_menus()
        ):
            self._canvas.end_drag()
            return True
        return super().eventFilter(watched, event)

    def _preview_tool(self, tool: str) -> None:
        """Name the tool under the cursor, without arming it.

        Reverts to the active tool on leave, so hovering is purely a read of
        what a button would do. Not while the style popover is open, which
        the strip would land on.
        """
        if (
            not self._canvas.is_annotating()
            or tool not in tokens.TOOL_HINTS
            or not self._style_popover.isHidden()
        ):
            return
        self._tool_hint.set_tool(tool)
        self._tool_hint.show()
        self._tool_hint.raise_()
        self._place_strip(self._tool_hint)

    def _place_strip(self, widget) -> None:
        size = widget.sizeHint()
        bar = self._bar.geometry()
        top = bar.top() - tokens.Metric.TRAY_OFFSET_Y - size.height()
        widget.setGeometry(
            round(bar.center().x() - size.width() / 2), round(max(0, top)),
            size.width(), size.height(),
        )

    def _sync_tool_hint(self) -> None:
        """Name the active tool by the bar while editing, unless the style
        popover is open -- the overlay's `_sync_tool_hint`.
        """
        tool = self._bar.active_tool
        # Gated on whether we are editing, not on whether the widget is
        # mapped: `isVisible()` is false for a window that has not been
        # shown yet, which would leave the strip down in every test and on
        # the first paint of a window opened programmatically.
        if not (self._canvas.is_annotating() and tool and self._style_popover.isHidden()):
            self._tool_hint.hide()
            return
        # Told the tool each time, so it never names whichever it showed last.
        strip = self._tool_hint
        strip.set_tool(tool)
        size = strip.sizeHint()
        bar = self._bar.geometry()
        strip.setGeometry(
            round(bar.center().x() - size.width() / 2),
            bar.bottom() + tokens.Metric.TRAY_OFFSET_Y,
            size.width(),
            size.height(),
        )
        # Below the bar would fall off the canvas floor here -- the bar
        # already sits 18px from it -- so it goes above instead.
        if strip.geometry().bottom() > self._canvas.height():
            strip.move(strip.x(), bar.top() - tokens.Metric.TRAY_OFFSET_Y - size.height())
        strip.show()
        strip.raise_()

    # -- footer ----------------------------------------------------------

    def _build_footer_contents(self) -> None:
        stacked = QVBoxLayout()
        stacked.setSpacing(1)
        self._status = QLabel()
        self._status.setFont(_ui_font(12, 500))
        stacked.addWidget(self._status)

        # Clickable, because users go looking for the file: the answer is in
        # the window and it is actionable.
        self._path_label = QPushButton()
        self._path_label.setFlat(True)
        self._path_label.setCursor(Qt.CursorShape.PointingHandCursor)
        self._path_label.setFont(_mono_font(11.5))
        self._path_label.setStyleSheet(
            f"QPushButton {{ border: none; background: transparent; text-align: left;"
            f" color: {tokens.Win.TEXT_MUTED}; padding: 0; }}"
            f"QPushButton:hover {{ color: {tokens.Win.TEXT_SECONDARY};"
            " text-decoration: underline; }"
        )
        self._path_label.clicked.connect(self.show_in_folder)
        stacked.addWidget(self._path_label)
        self.footer_left.addLayout(stacked)

        self._annotate_button = SecondaryButton("Edit")
        self._annotate_button.clicked.connect(
            lambda: self._set_annotating(not self._canvas._annotating)
        )
        self.footer_right.addWidget(self._annotate_button)

        self._folder_button = SecondaryButton("Show in Folder")
        self._folder_button.clicked.connect(self.show_in_folder)
        self._folder_button.setEnabled(self._saved_path is not None)
        self.footer_right.addWidget(self._folder_button)

        self._save_as_button = SecondaryButton("Save As…")
        self._save_as_button.clicked.connect(self.save_as)
        self.footer_right.addWidget(self._save_as_button)

        self._copy_button = AccentButton("Copy")
        self._copy_button.clicked.connect(self.copy)
        self.footer_right.addWidget(self._copy_button)

    def _show_toast(self, icon_name: str, text: str) -> None:
        """Confirm an action where the user is looking -- over the image,
        not in a footer line they have no reason to re-read.

        Placed clear of the floating bar rather than at the canvas floor.
        Both want bottom centre, so left to itself the toast landed
        underneath the bar and showed as a dark sliver poking out from it --
        unreadable, and easy to take for a rendering fault rather than a
        message. While editing it sits above the bar and whatever hangs off
        it; otherwise the floor is free and it uses it.
        """
        area = QRectF(self._canvas.rect())
        if self._bar.isVisible():
            highest = min(
                widget.geometry().top()
                for widget in (self._bar, self._style_popover, self._tool_hint)
                if widget.isVisible()
            )
            area.setBottom(highest - tokens.Metric.TRAY_OFFSET_Y)
        self._toast.show_message(icon_name, text, area)
        self._toast.raise_()

    def _refresh_status(self) -> None:
        if self._dirty:
            self._status.setText("✎  Edited — not saved")
            self._status.setStyleSheet(f"color: {tokens.Win.WARN_FG};")
        elif self._saved_path is not None:
            self._status.setText("✓  Saved")
            self._status.setStyleSheet(f"color: {tokens.Win.OK_STRONG};")
        else:
            self._status.setText("Copied to the clipboard — not saved to disk.")
            self._status.setStyleSheet(f"color: {tokens.Win.TEXT_MUTED};")
        self._path_label.setText(self._display_path(self._saved_path))
        self._path_label.setEnabled(self._saved_path is not None)

    def _on_edited(self) -> None:
        """Any change to the ink makes the window dirty -- the footer says
        so, and Copy or Save As is what clears it again.
        """
        self._dirty = True
        self._refresh_status()
        self._refresh_badges()

    # -- badges ----------------------------------------------------------

    def _refresh_badges(self) -> None:
        count = len(self._store)
        marks = "" if count == 0 else (
            "  ·  1 mark" if count == 1 else f"  ·  {count} marks"
        )
        self._dimension_badge.setText(
            f"{self._image.width()} × {self._image.height()}{marks}"
        )
        # The canvas is the authority on what the zoom clamped to, so the
        # badge follows it rather than the two keeping separate counts.
        self._zoom_badge.set_zoom(self._canvas.zoom)
        self._dimension_badge.adjustSize()
        self._place_overlays()

    def _on_zoom_changed(self, percent: int) -> None:
        self._canvas.set_zoom(percent)
        self._refresh_badges()

    def _place_overlays(self) -> None:
        inset_h, inset_v = tokens.WinMetric.REVIEW_BADGE_INSET
        self._dimension_badge.move(inset_h, inset_v)
        self._zoom_badge.move(
            self._canvas.width() - inset_h - self._zoom_badge.width(), inset_v
        )
        bar = getattr(self, "_bar", None)
        if bar is not None and bar.isVisible():
            size = bar.sizeHint()
            bar.setGeometry(
                round((self._canvas.width() - size.width()) / 2),
                self._canvas.height() - tokens.WinMetric.REVIEW_BAR_BOTTOM - size.height(),
                size.width(),
                size.height(),
            )

    def keyPressEvent(self, event) -> None:
        """Escape abandons a label being typed, then leaves edit mode, then
        closes -- the same staged retreat the overlay offers, so a
        half-typed label never costs the window.
        """
        if event.key() == Qt.Key.Key_Escape:
            if self._canvas.has_active_label():
                self._canvas.abandon_text()
                return
            # An open menu is what Esc closes first.
            if self._close_bar_menus():
                return
            if self._canvas.is_annotating():
                self._set_annotating(False)
                return
        # The bar's tool and style keys, while the bar is up, as on the
        # overlay. A label being typed into keeps its own letters, and a
        # letter held with a modifier is somebody else's shortcut.
        elif (
            self._canvas.is_annotating()
            and event.modifiers() == Qt.KeyboardModifier.NoModifier
            and (
                self._bar.handle_tool_key(event.key())
                or self._style_popover.handle_key(event.key())
            )
        ):
            return
        super().keyPressEvent(event)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        # A menu is anchored to where its slot was, and the bar is moving.
        for menu in getattr(self, "_family_menus", {}).values():
            menu.hide()
        popover = getattr(self, "_style_popover", None)
        if popover is not None:
            popover.hide()
        self._place_overlays()

    # -- actions ---------------------------------------------------------

    def _set_annotating(self, annotating: bool) -> None:
        self._canvas.set_annotating(annotating)
        self._bar.setVisible(annotating)
        self._annotate_button.setText("Done editing" if annotating else "Edit")
        if not annotating:
            self._close_bar_menus()
        self._place_overlays()
        self._sync_tool_hint()

    @staticmethod
    def _display_path(path: Path | None) -> str:
        r"""`~`-relative where possible: most of a screenshot's path is the
        user's own home directory read back at them.

        `os.sep`, not a literal "/": `relative_to()` renders with the
        platform's own separator, so hardcoding the Unix one produced
        "~/Pictures\snipux\Screenshot from ....png" on Windows -- one path
        spelled two ways in the same string, in the label whose whole job
        is telling the user where their file is.
        """
        if path is None:
            return "Not saved to disk"
        try:
            return f"~{os.sep}{path.relative_to(Path.home())}"
        except ValueError:
            return str(path)

    def copy(self) -> None:
        """Put the snip -- ink included -- on the clipboard, and clear the
        dirty state.

        Useful even for one copied on the way here: anything copied since
        has replaced it.
        """
        clipboard = QGuiApplication.clipboard()
        if clipboard is not None:
            clipboard.setImage(self._canvas.rendered_image())
        self._dirty = False
        self._refresh_status()
        self._show_toast("copy", "Copied to clipboard")

    def save_as(self, path: Path | str | None = None) -> Path | None:
        """Write the snip where the user picks. Returns the path, or None if
        cancelled.

        `path` is only ever passed by tests -- QFileDialog cannot be driven
        offscreen, and mocking Qt's own static method would test the mock.
        """
        if path is None:
            chosen, _ = QFileDialog.getSaveFileName(
                self, "Save snip", str(self._suggested_path()), "PNG image (*.png)"
            )
            if not chosen:
                return None
            path = chosen
        path = Path(path)
        # An extension-less name typed into the dialog would be written as a
        # PNG that nothing opens by double-click.
        if path.suffix == "":
            path = path.with_suffix(".png")
        path.parent.mkdir(parents=True, exist_ok=True)
        if not self._canvas.rendered_image().save(str(path), "PNG"):
            self._status.setText(f"Could not write {self._display_path(path)}")
            self._status.setStyleSheet(f"color: {tokens.Win.ERR_FG};")
            return None
        self._saved_path = path
        self._dirty = False
        self._folder_button.setEnabled(True)
        self.title_label.setText(path.name)
        self._refresh_status()
        self._show_toast("save", f"Saved to {self._display_path(path)}")
        return path

    def _suggested_path(self) -> Path:
        if self._saved_path is not None:
            return self._saved_path
        return setup_desktop.load_save_folder() / "snip.png"

    def show_in_folder(self) -> None:
        """Open the containing directory in the file manager.

        The directory, not the file: opening a PNG launches an image viewer,
        which is not what "show in folder" means anywhere else.
        """
        if self._saved_path is None:
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._saved_path.parent)))
