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
from dataclasses import replace
from pathlib import Path

from PyQt6.QtCore import QEvent, QPointF, QRectF, Qt, QUrl, pyqtSignal
from PyQt6.QtGui import (
    QColor,
    QDesktopServices,
    QDragEnterEvent,
    QDropEvent,
    QGuiApplication,
    QImage,
    QImageReader,
    QImageWriter,
    QKeySequence,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPixmap,
    QRadialGradient,
)
from PyQt6.QtWidgets import (
    QFileDialog,
    QLabel,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from . import design, output, setup_desktop, shapes
from .design import tokens
from .marks import (
    MarkStore,
    TextLabelEditor,
    ToolStyles,
    begin_stroke,
    extend_stroke,
    session_styles,
    snap_to_text,
)
from .overlay import Toast
from .popovers import FamilyMenu, StylePopover
from .toolbar import FloatingBar, ToolHintStrip
from .winchrome import (
    AccentButton,
    Badge,
    EmptyState,
    SecondaryButton,
    WinWindow,
    ZoomBadge,
    _mono_font,
    _ui_font,
    open_filter,
)

IMAGE_FILTER = open_filter("Images", tokens.EDITOR_IMAGE_SUFFIXES)


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

    def _to_widget(self, point: QPointF) -> QPointF:
        """Image point -> widget coordinates -- `to_image`'s own inverse,
        for the one caller (`_start_callout_text_entry`) that has to place
        a widget (the text editor's field) at a mark's own image-space
        point rather than read a click.
        """
        rect = self.image_rect()
        scale = self._scale() or 1.0
        return QPointF(rect.x() + point.x() * scale, rect.y() + point.y() * scale)

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
        # A label still open is on screen and belongs in the picture: the
        # bar's Copy takes no focus, so nothing else would have ended it.
        self._text_editor.commit()
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
        if isinstance(finished, shapes.Highlighter) and self._styles.of("highlighter").snap == "text":
            # Marks here are already in the image's pixels: nothing to scale.
            finished = snap_to_text(finished, self._image)
        if isinstance(finished, shapes.Callout):
            # Not added yet -- see `_start_callout_text_entry` and
            # `shapes.Callout`'s own docstring for why body/tail/text
            # become one mark only once its text editor commits.
            self._start_callout_text_entry(finished)
        elif finished is not None:
            self._store.add(finished)
            self.marksChanged.emit()
        self.update()

    def _start_callout_text_entry(self, shape: shapes.Callout) -> None:
        """The overlay's own `_start_callout_text_entry`, mirrored here for
        Annotate mode: open the label editor over the just-dragged
        `Callout`'s body (mapped back to widget coordinates -- marks here
        are in image space, the field is not), and add the mark, once,
        when it commits.
        """
        self._text_editor.begin(
            self._to_widget(shape.body_rect().topLeft()),
            shape.colour,
            shape.stroke_width,
            on_commit=lambda text: self._commit_callout(shape, text),
        )

    def _commit_callout(self, shape: shapes.Callout, text: str) -> None:
        self._store.add(replace(shape, text=text))
        self.marksChanged.emit()

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
            # Every spotlight in one pass, where the first one sits: one dim
            # with several holes, as the saved file has. Taken one at a
            # time, each dimmed the others' holes.
            spotlights = [s for s in obscuring if isinstance(s, shapes.Spotlight)]
            spotlights_done = False
            for shape in obscuring:
                if isinstance(shape, shapes.Spotlight):
                    if not spotlights_done:
                        result = shapes.Spotlight.apply_all(result, spotlights)
                        spotlights_done = True
                    continue
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

    def set_image(self, image: QImage, store: MarkStore) -> None:
        """Swap in a new document -- what a file dropped onto the review
        window does, in place of opening a second one.

        Every bit of state this canvas keeps about the old image resets
        with it: the composited-image cache (keyed on the old marks, which
        are gone), the zoom (a new image is fit to the canvas fresh, not
        left at whatever the last one was zoomed to), and any drag or label
        the old image had in progress.
        """
        self._in_progress = None
        self._erasing = False
        self._text_editor.abandon()
        self._text_editor.setParent(None)
        self._text_editor.deleteLater()

        self._image = image
        self._store = store
        self._composite_key = None
        self._composite = image
        self._zoom = 100
        self._tool = None
        self._text_editor = TextLabelEditor(self, store, to_document=self.to_image)
        self._store.changed.connect(self.update)
        self.update()


class ReviewWindow(WinWindow):
    """The window a finished snip opens in.

    `saved_path` is where it already went, when it was saved rather than
    copied. Its absence is why `Show in Folder` starts disabled rather than
    hidden -- the window should not change shape depending on how the snip
    arrived.

    With no `image` it is the image editor opened on its own (`snipux
    --image-editor`): an empty state where the canvas goes, until Open…
    or a dropped file gives it something to show.
    """

    # A recording dropped on this window: the controller decides where it
    # goes. Unconnected (a window built on its own), the drop is refused
    # here as any other file this window cannot open.
    otherEditorRequested = pyqtSignal(object)

    def __init__(
        self,
        image: QImage | None = None,
        *,
        saved_path: Path | None = None,
        parent: QWidget | None = None,
    ):
        super().__init__(
            "Snipux",
            size=(tokens.WinMetric.REVIEW_W, tokens.WinMetric.REVIEW_H),
            parent=parent,
        )
        self._has_image = image is not None
        if image is None:
            # The canvas needs something to hold until a file arrives; it is
            # never on screen while this stands in.
            image = QImage(1, 1, QImage.Format.Format_ARGB32)
            image.fill(Qt.GlobalColor.transparent)
        self._image = image
        self._saved_path = saved_path
        self._dirty = False
        self._store = MarkStore(self)

        if self._has_image:
            self.set_title(
                saved_path.name if saved_path is not None else "Unsaved snip"
            )
            self.title_detail.setText(f"{image.width()} × {image.height()}")
        else:
            self.set_title("Image Editor")

        body = QVBoxLayout(self.body)
        body.setContentsMargins(0, 0, 0, 0)
        self._pages = QStackedWidget()
        body.addWidget(self._pages)
        self._empty = EmptyState(
            "image",
            "Open an image to edit",
            "Mark up, blur or pixelate any screenshot, then copy it or save it.",
        )
        self._empty.openRequested.connect(self.choose_file)
        self._pages.addWidget(self._empty)

        self._styles = session_styles
        # A review window can be the first thing a session opens (a file
        # handed to `snipux`), so it seeds from Settings too.
        self._styles.configure(*setup_desktop.load_style_defaults())
        self._canvas = ImageCanvas(image, self._store, styles=self._styles)
        self._canvas.marksChanged.connect(self._on_edited)
        self._pages.addWidget(self._canvas)

        self._dimension_badge = Badge(self._canvas)
        self._zoom_badge = ZoomBadge(self._canvas)
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
        self._store.changed.connect(self._sync_undo_buttons)

        self._build_footer_contents()
        self._show_image_page(self._has_image)
        self._refresh_status()
        # A colleague's screenshot dropped onto this window opens it here,
        # the same as `snipux PATH` does over the socket -- see `open_path`.
        self.setAcceptDrops(True)

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

        self._open_button = SecondaryButton("Open…")
        self._open_button.setToolTip("Open another image in this window (Ctrl+O)")
        self._open_button.clicked.connect(self.choose_file)
        self.footer_right.addWidget(self._open_button)

        self._annotate_button = SecondaryButton("Edit")
        self._annotate_button.clicked.connect(
            lambda: self._set_annotating(not self._canvas._annotating)
        )
        self.footer_right.addWidget(self._annotate_button)

        self._folder_button = SecondaryButton("Show in Folder")
        self._folder_button.clicked.connect(self.show_in_folder)
        self._folder_button.setEnabled(self._saved_path is not None)
        self.footer_right.addWidget(self._folder_button)

        self._save_button = SecondaryButton("Save")
        self._save_button.clicked.connect(self.save)
        self.footer_right.addWidget(self._save_button)

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

    @property
    def is_empty(self) -> bool:
        """Opened on its own, with no image loaded into it yet."""
        return not self._has_image

    def _show_image_page(self, has_image: bool) -> None:
        """The canvas and everything that acts on it, or the empty state
        with all of that greyed out -- there is nothing to copy or save
        until an image is open.
        """
        self._has_image = has_image
        self._pages.setCurrentWidget(self._canvas if has_image else self._empty)
        for button in (
            self._annotate_button,
            self._save_button,
            self._save_as_button,
            self._copy_button,
        ):
            button.setEnabled(has_image)
        self._folder_button.setEnabled(has_image and self._saved_path is not None)
        self._dimension_badge.setVisible(has_image)
        self._zoom_badge.setVisible(has_image)

    def _refresh_status(self) -> None:
        if not self._has_image:
            self._status.setText("No image open")
            self._status.setStyleSheet(f"color: {tokens.Win.TEXT_MUTED};")
        elif self._dirty:
            self._status.setText("✎  Edited — not saved")
            self._status.setStyleSheet(f"color: {tokens.Win.WARN_FG};")
        elif self._saved_path is not None:
            self._status.setText("✓  Saved")
            self._status.setStyleSheet(f"color: {tokens.Win.OK_STRONG};")
        else:
            self._status.setText("Copied to the clipboard — not saved to disk.")
            self._status.setStyleSheet(f"color: {tokens.Win.TEXT_MUTED};")
        self._path_label.setText(
            self._display_path(self._saved_path) if self._has_image else ""
        )
        self._path_label.setEnabled(self._saved_path is not None)

    def _sync_undo_buttons(self) -> None:
        """The bar builds Undo and Redo disabled and waits to be told. This
        window never told it, so both stayed grey however much was drawn,
        and Clear could not be taken back."""
        self._bar.set_undo_enabled(self._store.can_undo)
        self._bar.set_redo_enabled(self._store.can_redo)

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
        if event.matches(QKeySequence.StandardKey.Open) and not self._canvas.has_active_label():
            self.choose_file()
            return
        # The bar's tooltips promise these. A label being typed keeps its
        # own Ctrl+Z, which undoes typing.
        if not self._canvas.has_active_label():
            control = Qt.KeyboardModifier.ControlModifier
            shift = Qt.KeyboardModifier.ShiftModifier
            modifiers = event.modifiers()
            if event.key() == Qt.Key.Key_Z and modifiers == control | shift:
                self._store.redo()
                return
            if event.key() == Qt.Key.Key_Y and modifiers == control:
                self._store.redo()
                return
            if event.key() == Qt.Key.Key_Z and modifiers == control:
                self._store.undo()
                return
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

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if any(url.isLocalFile() for url in event.mimeData().urls()):
            event.acceptProposedAction()
            self._empty.set_drag_active(True)

    def dragLeaveEvent(self, event) -> None:
        self._empty.set_drag_active(False)

    def dropEvent(self, event: QDropEvent) -> None:
        self._empty.set_drag_active(False)
        urls = [url for url in event.mimeData().urls() if url.isLocalFile()]
        if not urls:
            return
        event.acceptProposedAction()
        # The first, where several land at once: one window opens one image,
        # the same as `snipux PATH` only ever names one.
        path = Path(urls[0].toLocalFile())
        if (
            path.suffix.lower() in tokens.EDITOR_RECORDING_SUFFIXES
            and self.receivers(self.otherEditorRequested) > 0
        ):
            self.otherEditorRequested.emit(path)
            return
        self.open_path(path)

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
        """`~`-relative where possible: most of a screenshot's path is the
        user's own home directory read back at them.

        Kept as a method because this window calls it in six places, but the
        rule itself now lives in `output.display_path` -- the player and the
        overlay show the same kind of path and each had grown their own
        copy of it.
        """
        return output.display_path(path)

    def copy(self) -> None:
        """Put the snip -- ink included -- on the clipboard, and clear the
        dirty state.

        Useful even for one copied on the way here: anything copied since
        has replaced it.
        """
        # `output.copy_image_to_clipboard`, not `QClipboard.setImage`
        # directly: that helper also settles the clipboard and hands the
        # image to `wl-copy`, which is what makes a copy outlive the process
        # on Wayland. Copying from here used to be the one Copy in the app
        # that vanished when Snipux quit.
        output.copy_image_to_clipboard(self._canvas.rendered_image())
        self._dirty = False
        self._refresh_status()
        self._show_toast("copy", "Copied to clipboard")

    def save(self) -> Path | None:
        """Write back to the file this snip already came from, in that
        file's own format -- what an opened image's `Save` does, as
        opposed to `Save As…`'s "pick somewhere new".

        Falls through to `save_as()` -- which always writes PNG -- when
        there is nowhere to write back to yet, or when the format at that
        path is one `QImageWriter` can only read: GIF, SVG, SVGZ, PDF and
        TGA, per `QImageWriter.supportedImageFormats()`, are never in the
        set this checks against. No format list of our own to drift out of
        step with Qt's.
        """
        if self._saved_path is None:
            return self.save_as()
        image_format = self._saved_path.suffix.lstrip(".").lower()
        writable = {bytes(f).decode() for f in QImageWriter.supportedImageFormats()}
        if image_format not in writable:
            return self.save_as()
        if not self._canvas.rendered_image().save(str(self._saved_path), image_format.upper()):
            self._status.setText(f"Could not write {self._display_path(self._saved_path)}")
            self._status.setStyleSheet(f"color: {tokens.Win.ERR_FG};")
            return None
        self._dirty = False
        self._refresh_status()
        self._show_toast("save", f"Saved to {self._display_path(self._saved_path)}")
        return self._saved_path

    def save_as(self, path: Path | str | None = None) -> Path | None:
        """Write the snip where the user picks. Returns the path, or None if
        cancelled.

        `path` is only ever passed by tests -- QFileDialog cannot be driven
        offscreen, and mocking Qt's own static method would test the mock.
        """
        if path is None:
            chosen, _ = QFileDialog.getSaveFileName(
                self,
                "Save snip",
                str(self._suggested_path()),
                self._save_filters()
            )
            if not chosen:
                return None
            path = chosen
        path = Path(path)
        # An extension-less name typed into the dialog would be written as a
        # PNG that nothing opens by double-click.
        if path.suffix == "":
            path = path.with_suffix(".png")
        # The format is the one the name asks for. Always writing PNG put
        # PNG bytes in a file called .jpg, which the next Save then
        # rewrote as a real JPEG. A suffix Qt cannot write gets .png added
        # rather than a file that lies about what it is.
        image_format = path.suffix.lstrip(".").lower()
        writable = {bytes(f).decode() for f in QImageWriter.supportedImageFormats()}
        if image_format not in writable:
            path = path.with_name(path.name + ".png")
            image_format = "png"
        path.parent.mkdir(parents=True, exist_ok=True)
        if not self._canvas.rendered_image().save(str(path), image_format.upper()):
            self._status.setText(f"Could not write {self._display_path(path)}")
            self._status.setStyleSheet(f"color: {tokens.Win.ERR_FG};")
            return None
        self._saved_path = path
        self._dirty = False
        self._folder_button.setEnabled(True)
        self.set_title(path.name)
        self._refresh_status()
        self._show_toast("save", f"Saved to {self._display_path(path)}")
        return path

    @staticmethod
    def _save_filters() -> str:
        """PNG, then whichever of JPEG and WebP this Qt build can write."""
        writable = {bytes(f).decode() for f in QImageWriter.supportedImageFormats()}
        filters = ["PNG image (*.png)"]
        if "jpg" in writable:
            filters.append("JPEG image (*.jpg *.jpeg)")
        if "webp" in writable:
            filters.append("WebP image (*.webp)")
        return ";;".join(filters)

    def _suggested_path(self) -> Path:
        # A `.png` `_saved_path` is always a real suggestion -- Save As
        # writes PNG, and an ordinary snip's own path already sits in the
        # snips folder. An opened image in some other format is not: its
        # own path is where Save just failed to write, in a format the
        # dialog's "PNG image" filter cannot save anyway, so the snips
        # folder is the suggestion that is actually still true.
        if self._saved_path is not None and self._saved_path.suffix.lower() == ".png":
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

    def open_path(self, path: Path) -> None:
        """Load `path` into this window in place of what it currently
        shows -- a file dropped onto the window, rather than a second one
        opening next to it.

        Guarded by `_confirm_discard` exactly like Settings' Cancel guards
        discarding an edited settings pane: unsaved ink is not thrown away
        silently just because a new file landed on the window.
        """
        if self._dirty and not self._confirm_discard():
            return
        reader = QImageReader(str(path))
        image = reader.read()
        if image.isNull():
            QMessageBox.warning(
                self, "Could not open image", f"{path.name}: {reader.errorString()}"
            )
            return
        self._set_annotating(False)
        self._image = image
        self._saved_path = path
        self._dirty = False
        self._bar.undoRequested.disconnect()
        self._bar.redoRequested.disconnect()
        self._bar.clearRequested.disconnect()
        self._store.setParent(None)
        self._store.deleteLater()
        self._store = MarkStore(self)
        self._store.changed.connect(self._on_edited)
        self._store.changed.connect(self._sync_undo_buttons)
        self._sync_undo_buttons()
        self._bar.undoRequested.connect(self._store.undo)
        self._bar.redoRequested.connect(self._store.redo)
        self._bar.clearRequested.connect(self._store.clear)
        self._canvas.set_image(image, self._store)
        self.set_title(path.name)
        self.title_detail.setText(f"{image.width()} × {image.height()}")
        self._show_image_page(True)
        self._refresh_badges()
        self._refresh_status()

    def choose_file(self) -> None:
        """Open… and Ctrl+O: pick an image and load it into this window."""
        folder = (
            self._saved_path.parent
            if self._saved_path is not None
            else setup_desktop.load_save_folder()
        )
        chosen, _ = QFileDialog.getOpenFileName(
            self, "Open an image", str(folder), IMAGE_FILTER
        )
        if chosen:
            self.open_path(Path(chosen))

    def closeEvent(self, event) -> None:
        """Ask before closing takes unsaved ink with it. The close button
        and a second Esc both end here, and both used to drop every
        annotation without a word."""
        if self._dirty and not self._confirm_discard():
            event.ignore()
            return
        super().closeEvent(event)

    def _confirm_discard(self) -> bool:
        """The same "Discard changes?" prompt Settings' own Cancel shows
        over an edited-but-unsaved pane, asked here before a dropped file
        overwrites this window's unsaved ink.
        """
        answer = QMessageBox.question(
            self,
            "Discard changes?",
            "This snip has been edited but not saved. Discard the changes?",
            QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,
        )
        return answer == QMessageBox.StandardButton.Discard
