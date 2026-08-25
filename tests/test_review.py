"""Tests for `snipux/review.py` -- the review window from
`docs/design/handoff-windows.md` section 3.

The load-bearing claim these protect is that Annotate mode is not a second
editor: it drives the overlay's own `FloatingBar` and the same `MarkStore`,
and the only differences the design allows are the missing capture chip and
the `Done` trailing action.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PyQt6.QtCore import QPointF, Qt
from PyQt6.QtGui import QColor, QImage
from PyQt6.QtWidgets import QApplication

from snipux import shapes
from snipux.design import tokens
from snipux.overlay import FloatingBar
from snipux.review import ImageCanvas, ReviewWindow


@pytest.fixture(scope="module", autouse=True)
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def make_image(width=800, height=600) -> QImage:
    image = QImage(width, height, QImage.Format.Format_RGB32)
    image.fill(0x336699)
    return image


def _press(widget, x, y):
    from PyQt6.QtGui import QMouseEvent
    from PyQt6.QtCore import QEvent, QPointF
    return QMouseEvent(
        QEvent.Type.MouseButtonPress, QPointF(x, y), QPointF(x, y),
        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )


def _move(widget, x, y):
    from PyQt6.QtGui import QMouseEvent
    from PyQt6.QtCore import QEvent, QPointF
    return QMouseEvent(
        QEvent.Type.MouseMove, QPointF(x, y), QPointF(x, y),
        Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )


def _release(widget, x, y):
    from PyQt6.QtGui import QMouseEvent
    from PyQt6.QtCore import QEvent, QPointF
    return QMouseEvent(
        QEvent.Type.MouseButtonRelease, QPointF(x, y), QPointF(x, y),
        Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
    )


class TestChrome:
    def test_is_the_size_the_design_specifies(self):
        window = ReviewWindow(make_image())

        assert window.width() == tokens.WinMetric.REVIEW_W
        assert window.height() == tokens.WinMetric.REVIEW_H

    def test_the_title_bar_names_the_file_and_its_pixel_size(self, tmp_path):
        window = ReviewWindow(make_image(1377, 936), saved_path=tmp_path / "shot.png")

        assert window.title_label.text() == "shot.png"
        assert window.title_detail.text() == "1377 × 936"

    def test_an_unsaved_snip_says_so_rather_than_naming_a_file(self):
        window = ReviewWindow(make_image())

        assert window.title_label.text() == "Unsaved snip"


class TestStatusAndPath:
    def test_a_saved_snip_reads_saved(self, tmp_path):
        window = ReviewWindow(make_image(), saved_path=tmp_path / "shot.png")

        assert "Saved" in window._status.text()

    def test_a_copied_snip_says_it_is_not_on_disk(self):
        # The distinction that matters: a copied snip is one clipboard write
        # away from being gone.
        window = ReviewWindow(make_image())

        assert "not saved" in window._status.text().lower()

    def test_paths_under_home_are_shown_relative_to_it(self):
        path = Path.home() / "Pictures" / "snipux" / "shot.png"

        assert ReviewWindow._display_path(path) == "~/Pictures/snipux/shot.png"

    def test_show_in_folder_is_disabled_until_there_is_a_file(self, tmp_path):
        assert not ReviewWindow(make_image())._folder_button.isEnabled()
        assert ReviewWindow(
            make_image(), saved_path=tmp_path / "shot.png"
        )._folder_button.isEnabled()


class TestBadges:
    def test_the_badge_carries_the_real_pixel_size(self):
        window = ReviewWindow(make_image(1377, 936))

        assert "1377 × 936" in window._dimension_badge.text()

    def test_one_mark_is_singular(self):
        window = ReviewWindow(make_image())
        window._store.add(
            shapes.Pen(colour=QColor("#fff"), stroke_width=4, points=[QPointF(1, 1)])
        )

        assert "1 mark" in window._dimension_badge.text()
        assert "1 marks" not in window._dimension_badge.text()

    def test_several_marks_are_plural(self):
        window = ReviewWindow(make_image())
        for _ in range(3):
            window._store.add(
                shapes.Pen(colour=QColor("#fff"), stroke_width=4, points=[QPointF(1, 1)])
            )

        assert "3 marks" in window._dimension_badge.text()

    def test_no_marks_means_no_mark_clause_at_all(self):
        window = ReviewWindow(make_image())

        assert "mark" not in window._dimension_badge.text()


class TestAnnotateMode:
    """The design's central constraint: this reveals the overlay's own bar,
    and must not become a second editor.
    """

    def test_it_uses_the_overlays_own_floating_bar(self):
        window = ReviewWindow(make_image())

        assert isinstance(window._bar, FloatingBar)

    def test_the_bar_is_hidden_until_annotate_is_pressed(self):
        window = ReviewWindow(make_image())
        assert not window._bar.isVisibleTo(window)

        window._set_annotating(True)

        assert window._bar.isVisibleTo(window)

    def test_the_button_becomes_done_editing(self):
        window = ReviewWindow(make_image())

        window._set_annotating(True)

        assert window._annotate_button.text() == "Done editing"

    def test_there_is_no_capture_mode_chip(self):
        # Nothing left to capture, so the chip the overlay carries is absent
        # -- one of exactly two differences the design permits.
        window = ReviewWindow(make_image())

        assert not window._bar._chip.isVisibleTo(window._bar)

    def test_the_trailing_action_is_done_not_save(self):
        # The footer already owns the exports.
        window = ReviewWindow(make_image())

        assert window._bar._trailing == "done"

    def test_drawing_flips_the_status_to_edited(self):
        window = ReviewWindow(make_image())
        window._set_annotating(True)

        window._store.add(
            shapes.Pen(colour=QColor("#fff"), stroke_width=4, points=[QPointF(1, 1)])
        )

        assert "Edited" in window._status.text()

    def test_undo_and_redo_run_through_the_shared_store(self):
        window = ReviewWindow(make_image())
        window._store.add(
            shapes.Pen(colour=QColor("#fff"), stroke_width=4, points=[QPointF(1, 1)])
        )

        window._bar.undoRequested.emit()
        assert len(window._store) == 0

        window._bar.redoRequested.emit()
        assert len(window._store) == 1


class TestImageCoordinates:
    """Marks live in image space here, not screen space, because the image
    is the document -- so they survive a zoom and export where they looked.
    """

    def _canvas(self, image=None):
        from snipux.marks import MarkStore

        canvas = ImageCanvas(image or make_image(800, 600), MarkStore())
        canvas.resize(1020, 600)
        return canvas

    def test_the_centre_of_the_widget_is_the_centre_of_the_image(self):
        canvas = self._canvas()
        rect = canvas.image_rect()

        centre = canvas.to_image(rect.center())

        assert centre.x() == pytest.approx(400, abs=1)
        assert centre.y() == pytest.approx(300, abs=1)

    def test_the_same_pointer_position_maps_differently_at_a_different_zoom(self):
        # If it did not, ink drawn at 140% would land somewhere else on the
        # exported image.
        canvas = self._canvas()
        before = canvas.to_image(QPointF(500, 300))

        canvas.set_zoom(140)
        after = canvas.to_image(QPointF(500, 300))

        assert before != after

    def test_the_image_centre_stays_the_image_centre_at_any_zoom(self):
        canvas = self._canvas()

        for zoom in (60, 100, 160):
            canvas.set_zoom(zoom)
            centre = canvas.to_image(canvas.image_rect().center())
            assert centre.x() == pytest.approx(400, abs=1)

    def test_zoom_is_clamped_to_the_range_the_design_offers(self):
        canvas = self._canvas()
        low, high, _step = tokens.WinMetric.ZOOM_STEPS

        canvas.set_zoom(5)
        assert canvas.zoom == low

        canvas.set_zoom(500)
        assert canvas.zoom == high


class TestExport:
    def test_save_as_writes_a_real_png(self, tmp_path):
        window = ReviewWindow(make_image(120, 90))
        target = tmp_path / "chosen.png"

        assert window.save_as(target) == target
        reloaded = QImage(str(target))
        assert (reloaded.width(), reloaded.height()) == (120, 90)

    def test_save_as_adds_a_png_extension_when_none_was_typed(self, tmp_path):
        window = ReviewWindow(make_image())

        written = window.save_as(tmp_path / "no-extension")

        assert written == tmp_path / "no-extension.png"
        assert written.exists()

    def test_save_as_creates_a_missing_directory(self, tmp_path):
        window = ReviewWindow(make_image())
        target = tmp_path / "not" / "there" / "shot.png"

        assert window.save_as(target) == target

    def test_saving_clears_the_dirty_state_and_enables_the_folder_button(self, tmp_path):
        window = ReviewWindow(make_image())
        window._store.add(
            shapes.Pen(colour=QColor("#fff"), stroke_width=4, points=[QPointF(1, 1)])
        )
        assert "Edited" in window._status.text()

        window.save_as(tmp_path / "shot.png")

        assert "Saved" in window._status.text()
        assert window._folder_button.isEnabled()

    def test_copy_clears_the_dirty_state(self):
        window = ReviewWindow(make_image(64, 64))
        window._store.add(
            shapes.Pen(colour=QColor("#fff"), stroke_width=4, points=[QPointF(1, 1)])
        )

        window.copy()

        assert "Edited" not in window._status.text()

    def test_the_export_carries_the_ink(self, tmp_path):
        # Marks are already in image coordinates, so the export needs no
        # translation -- which is most of why the design puts them there.
        window = ReviewWindow(make_image(200, 200))
        window._store.add(
            shapes.Rectangle(
                colour=QColor("#ff0000"),
                stroke_width=8,
                start=QPointF(20, 20),
                end=QPointF(180, 180),
            )
        )

        target = window.save_as(tmp_path / "inked.png")

        reloaded = QImage(str(target))
        colours = {reloaded.pixelColor(x, 20).name() for x in range(20, 180)}
        assert "#ff0000" in colours, "the rectangle should be in the exported pixels"


class TestEveryToolSurvivesAPaint:
    """The crash: Blur and Pixelate are `ObscuringShape`s, which sample
    already-rendered pixels through `apply()` and raise from `draw()`. The
    canvas called `draw()` on every mark, so reaching for the blur tool
    took the window down.
    """

    def _drawn(self, tool: str) -> ReviewWindow:
        from snipux.marks import MarkStore

        window = ReviewWindow(make_image(600, 400))
        window.resize(1020, 700)
        window._set_annotating(True)
        window._canvas.resize(1020, 600)
        window._canvas.set_tool(tool)
        canvas = window._canvas
        canvas.mousePressEvent(_press(canvas, 400, 300))
        canvas.mouseMoveEvent(_move(canvas, 500, 380))
        canvas.mouseReleaseEvent(_release(canvas, 500, 380))
        return window

    @pytest.mark.parametrize("tool", ["pen", "highlighter", "arrow", "rect", "step", "blur"])
    def test_drawing_then_painting_does_not_raise(self, tool):
        window = self._drawn(tool)

        window._canvas.grab()  # a full paintEvent over the committed mark

        assert len(window._store) == 1

    def test_an_obscuring_mark_is_baked_rather_than_drawn(self):
        # Painting it would raise; it has to go through the composite.
        window = self._drawn("blur")

        composite = window._canvas._composited()

        assert composite is not window._canvas._image

    def test_the_composite_is_cached_between_paints(self):
        # Re-sampling every obscuring mark on every frame would stall.
        window = self._drawn("blur")
        first = window._canvas._composited()

        assert window._canvas._composited() is first

    def test_the_composite_is_recomputed_when_marks_change(self):
        window = self._drawn("blur")
        first = window._canvas._composited()

        window._store.undo()

        assert window._canvas._composited() is not first


class TestTraysAreReachable:
    """The other half of the report: the tools were selectable but not
    configurable -- no pen size, no brush size, no colour -- because the
    trays that set those live on the overlay window, and the review window
    had none.
    """

    def _editing(self) -> ReviewWindow:
        window = ReviewWindow(make_image())
        window.resize(1020, 700)
        window._set_annotating(True)
        return window

    def test_a_draw_tool_shows_the_colour_and_stroke_tray(self):
        window = self._editing()

        window._bar.select_tool("pen")
        window._sync_tray()

        assert window._tray.isVisibleTo(window._canvas)

    def test_blur_replaces_it_with_the_blur_tray(self):
        # At most one is ever up: it replaces the tray rather than joining it.
        window = self._editing()

        window._bar.select_tool("blur")
        window._sync_tray()

        assert window._blur_tray.isVisibleTo(window._canvas)
        assert not window._tray.isVisibleTo(window._canvas)

    def test_the_eraser_gets_neither(self):
        window = self._editing()

        window._bar.select_tool("eraser")
        window._sync_tray()

        assert not window._tray.isVisibleTo(window._canvas)
        assert not window._blur_tray.isVisibleTo(window._canvas)

    def test_the_stroke_slider_actually_changes_the_stroke(self):
        window = self._editing()

        window._tray.strokeChanged.emit(22)

        assert window._canvas._stroke_width == 22

    def test_the_swatches_actually_change_the_colour(self):
        window = self._editing()

        window._tray.colourChanged.emit("#ff0000")

        assert window._canvas._ink_colour == "#ff0000"

    def test_blur_strength_and_mode_reach_the_canvas(self):
        window = self._editing()

        window._blur_tray.strengthChanged.emit(14)
        window._blur_tray.blurModeChanged.emit("pixelate")

        assert window._canvas._blur_strength == 14
        assert window._canvas._blur_mode == "pixelate"

    def test_leaving_edit_mode_puts_the_trays_away(self):
        window = self._editing()
        window._bar.select_tool("pen")
        window._sync_tray()

        window._set_annotating(False)

        assert not window._tray.isVisibleTo(window._canvas)


class TestEditLabel:
    def test_the_button_says_edit_not_annotate(self):
        assert ReviewWindow(make_image())._annotate_button.text() == "Edit"

    def test_and_done_editing_while_editing(self):
        window = ReviewWindow(make_image())

        window._set_annotating(True)

        assert window._annotate_button.text() == "Done editing"
