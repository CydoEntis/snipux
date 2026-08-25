"""The review window: what a snip looks like once the overlay has closed.

`docs/design/handoff-windows.md` section 3 is the authority. 1020 x 700, the
same `Win` chrome as Settings, and a canvas that answers the two questions
the flow could not: **what did I capture**, and **where did it go**.

Annotate mode reveals *the overlay's own floating bar* over the image -- the
same widget, the same tools, the same `MarkStore`. It is not a second
editor and must not become one; the only differences the design allows are
that there is no capture-mode chip (nothing left to capture) and the
trailing action is `Done` rather than Save (the footer already owns the
exports).

The one real divergence from the overlay is coordinates: marks live in
**image** space here, not screen space, because the image is the document.
`ImageCanvas` maps pointer positions through the current zoom, so drawing
stays correct at any magnification and an exported mark lands where it
looked.
"""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import QPoint, QPointF, QRect, QRectF, Qt, QUrl, pyqtSignal
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
from .marks import MarkStore, TextLabelEditor, begin_stroke, extend_stroke
from .overlay import BlurTray, FloatingBar, SettingsTray, ShapeToolPopover
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

    def __init__(self, image: QImage, store: MarkStore, parent: QWidget | None = None):
        super().__init__(parent)
        self._image = image
        self._store = store
        self._zoom = 100
        self._annotating = False
        self._tool: str | None = None
        self._ink_colour = tokens.INK_SWATCHES[0][1]
        self._stroke_width = tokens.Metric.STROKE_DEFAULT
        self._blur_mode = "blur"
        self._blur_strength = tokens.Metric.BLUR_DEFAULT
        self._in_progress: shapes.Shape | None = None
        self._composite_key: tuple | None = None
        self._composite: QImage = image
        # The same label editor the overlay uses. `to_image` is the only
        # difference: the field is placed at the widget point clicked, but
        # the mark it commits is stored in image coordinates, like every
        # other mark here.
        self._text_editor = TextLabelEditor(self, store, to_document=self.to_image)
        self.setMouseTracking(True)
        self._store.changed.connect(self.update)

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

    def set_ink_colour(self, colour: str) -> None:
        self._ink_colour = colour

    def set_stroke_width(self, stroke: int) -> None:
        self._stroke_width = stroke

    def set_blur_mode(self, mode: str) -> None:
        self._blur_mode = mode

    def set_blur_strength(self, strength: int) -> None:
        self._blur_strength = strength

    def rendered_image(self) -> QImage:
        """The image with every mark flattened onto it.

        Marks are already in image coordinates, so unlike the overlay's own
        export there is no translation step -- which is most of why the
        design puts them in this space.
        """
        return shapes.render(self._image, list(self._store.marks))

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if not self._annotating or event.button() != Qt.MouseButton.LeftButton:
            return
        position = self.to_image(event.position())
        if self._tool == "eraser":
            self._store.erase(position)
            return
        if self._tool == "text":
            # Placed where the click landed, in widget coordinates -- the
            # editor converts to image coordinates for the mark itself.
            self._text_editor.begin(
                event.position(), QColor(self._ink_colour), self._stroke_width
            )
            return
        # Any other tool ends a label still being typed, rather than
        # abandoning it: a click elsewhere never blurs the field, so nothing
        # else would force the commit.
        self._text_editor.commit()
        if self._tool == "step":
            # Click only, no drag -- the same rule the overlay applies.
            self._store.add(
                shapes.StepMarker(
                    colour=QColor(self._ink_colour),
                    stroke_width=self._stroke_width,
                    point=position,
                    number=shapes.next_step_number(list(self._store.marks)),
                )
            )
            self.marksChanged.emit()
            return
        self._in_progress = begin_stroke(
            self._tool,
            position,
            colour=QColor(self._ink_colour),
            stroke_width=self._stroke_width,
            blur_mode=self._blur_mode,
            blur_strength=self._blur_strength,
        )
        self.update()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._in_progress is None:
            return
        extend_stroke(self._in_progress, self.to_image(event.position()))
        self.update()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if self._in_progress is None:
            return
        shape, self._in_progress = self._in_progress, None
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
            "snipux",
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

        self._canvas = ImageCanvas(image, self._store)
        self._canvas.marksChanged.connect(self._on_edited)
        body.addWidget(self._canvas)

        self._dimension_badge = _Badge(self._canvas)
        self._zoom_badge = _Badge(self._canvas)
        self._refresh_badges()

        self._bar = FloatingBar(self._canvas, capture_chip=False, trailing="done")
        self._bar.hide()
        self._bar.toolSelected.connect(self._on_tool_selected)
        self._bar.undoRequested.connect(self._store.undo)
        self._bar.redoRequested.connect(self._store.redo)
        self._bar.clearRequested.connect(self._store.clear)
        self._bar.copyRequested.connect(self.copy)
        self._bar.saveRequested.connect(lambda: self._set_annotating(False))
        self._bar.shapeMenuRequested.connect(self._toggle_shape_popover)

        # The same trays the overlay shows, instantiated here rather than
        # reimplemented: they are where colour, stroke width, blur mode and
        # blur strength are actually set, and without them the bar's tools
        # were selectable but unconfigurable -- no pen size, no brush size,
        # no colour. `_sync_tray` shows at most one, exactly as the overlay
        # does.
        self._tray = SettingsTray(self._canvas)
        self._tray.hide()
        self._tray.colourChanged.connect(self._canvas.set_ink_colour)
        self._tray.strokeChanged.connect(self._canvas.set_stroke_width)

        self._blur_tray = BlurTray(self._canvas)
        self._blur_tray.hide()
        self._blur_tray.blurModeChanged.connect(self._canvas.set_blur_mode)
        self._blur_tray.strengthChanged.connect(self._canvas.set_blur_strength)

        # The rect button's Ellipse/Line/Crop submenu, for the same reason:
        # three of the tools were unreachable without it.
        self._shape_popover = ShapeToolPopover(self._canvas)
        self._shape_popover.hide()
        self._shape_popover.toolSelected.connect(self._on_shape_selected)

        self._store.changed.connect(self._on_edited)

        self._build_footer_contents()
        self._refresh_status()

    def _on_tool_selected(self, tool: str) -> None:
        self._canvas.set_tool(tool)
        self._shape_popover.hide()
        self._sync_tray()

    def _on_shape_selected(self, shape: str) -> None:
        self._shape_popover.hide()
        self._bar.select_tool(shape)

    def _toggle_shape_popover(self) -> None:
        if self._shape_popover.isVisible():
            self._shape_popover.hide()
            return
        active = self._bar.active_tool
        self._shape_popover.set_tool(
            active if active in tokens.RECT_GROUP else tokens.RECT_GROUP[0]
        )
        button = self._bar._tool_buttons["rect"]
        origin = button.mapTo(self._canvas, QPoint(0, 0))
        self._shape_popover.reposition(
            QRect(origin, button.size()), QRectF(self._canvas.rect())
        )
        self._shape_popover.show()
        self._shape_popover.raise_()

    def _sync_tray(self) -> None:
        """Show whichever tray matches the active tool, or neither.

        At most one is ever visible, per the spec's "it replaces the colour
        and stroke tray rather than sitting alongside it".
        """
        tool = self._bar.active_tool
        for tray in (self._tray, self._blur_tray):
            tray.hide()
        # Gated on whether we are editing, not on whether the widget is
        # mapped: `isVisible()` is false for a window that has not been
        # shown yet, which would leave the trays down in every test and on
        # the first paint of a window opened programmatically.
        if not self._canvas.is_annotating():
            return
        tray = None
        if tool in tokens.DRAW_TOOLS:
            # The tray names the active tool and carries its hint, so it has
            # to be told which one -- otherwise it keeps whichever it was
            # last showing and reads as the wrong tool entirely.
            self._tray.set_tool(tool)
            tray = self._tray
        elif tool == "blur":
            tray = self._blur_tray
        if tray is None:
            return
        size = tray.sizeHint()
        bar = self._bar.geometry()
        tray.setGeometry(
            round(bar.center().x() - size.width() / 2),
            bar.bottom() + tokens.Metric.TRAY_OFFSET_Y,
            size.width(),
            size.height(),
        )
        # Below the bar would fall off the canvas floor here -- the bar
        # already sits 18px from it -- so the tray goes above instead.
        if tray.geometry().bottom() > self._canvas.height():
            tray.move(tray.x(), bar.top() - tokens.Metric.TRAY_OFFSET_Y - size.height())
        tray.show()
        tray.raise_()

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
        self._zoom_badge.setText(f"−   {self._canvas.zoom}%   +")
        self._dimension_badge.adjustSize()
        self._zoom_badge.adjustSize()
        self._place_overlays()

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
            if self._canvas.is_annotating():
                self._set_annotating(False)
                return
        super().keyPressEvent(event)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._place_overlays()

    # -- actions ---------------------------------------------------------

    def _set_annotating(self, annotating: bool) -> None:
        self._canvas.set_annotating(annotating)
        self._bar.setVisible(annotating)
        self._annotate_button.setText("Done editing" if annotating else "Edit")
        if not annotating:
            self._shape_popover.hide()
        self._place_overlays()
        self._sync_tray()

    @staticmethod
    def _display_path(path: Path | None) -> str:
        """`~`-relative where possible: most of a screenshot's path is the
        user's own home directory read back at them.
        """
        if path is None:
            return "Not saved to disk"
        try:
            return f"~/{path.relative_to(Path.home())}"
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
