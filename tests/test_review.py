"""Tests for `snipux/review.py` -- the review window from
`docs/design/handoff-windows.md` section 3.

The load-bearing claim these protect is that Annotate mode is not a second
editor: it drives the overlay's own `FloatingBar` and the same `MarkStore`,
and the only differences the design allows are the missing capture chip and
the `Done` trailing action.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from PyQt6.QtCore import QEvent, QPointF, Qt
from PyQt6.QtGui import QColor, QFont, QImage, QMouseEvent, QPainter
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication

from snipux import shapes
from snipux.design import tokens
from snipux.marks import session_styles
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

        displayed = ReviewWindow._display_path(path)

        assert displayed == f"~{os.sep}Pictures{os.sep}snipux{os.sep}shot.png"

    def test_the_displayed_path_uses_one_separator_throughout(self):
        # This test used to normalise backslashes to forward slashes before
        # comparing, with a comment calling the mix expected -- so it passed
        # while the footer mixed the two separators in one path on Windows. `_display_path` hardcoded "~/" and let
        # `relative_to()` supply the platform's own separator for the rest,
        # spelling one path two ways in the label whose only job is saying
        # where the file is.
        displayed = ReviewWindow._display_path(
            Path.home() / "Pictures" / "snipux" / "shot.png"
        )

        foreign = "/" if os.sep == "\\" else "\\"
        assert foreign not in displayed

    def test_show_in_folder_is_disabled_until_there_is_a_file(self, tmp_path):
        assert not ReviewWindow(make_image())._folder_button.isEnabled()
        assert ReviewWindow(
            make_image(), saved_path=tmp_path / "shot.png"
        )._folder_button.isEnabled()


class TestZoomCluster:
    """The canvas's top-right zoom control.

    It shipped as a plain `_Badge`: the "−" and "+" were characters inside
    one QLabel's text with nothing behind them. `ImageCanvas.set_zoom()`
    was fully implemented and *nothing in the app ever called it*, so the
    control the handoff specifies as interactive (minus / percentage /
    plus, 60-160% in steps of 20) read "100%" and did nothing for the
    whole life of the window. These tests are about the picture actually
    changing, not the number.
    """

    def test_clicking_plus_actually_enlarges_the_drawn_image(self):
        window = ReviewWindow(make_image(400, 300))
        window.resize(1000, 700)
        before = window._canvas.image_rect().width()

        QTest.mouseClick(window._zoom_badge._plus, Qt.MouseButton.LeftButton)

        assert window._canvas.zoom == 120
        assert window._canvas.image_rect().width() > before

    def test_clicking_minus_actually_shrinks_the_drawn_image(self):
        window = ReviewWindow(make_image(400, 300))
        window.resize(1000, 700)
        before = window._canvas.image_rect().width()

        QTest.mouseClick(window._zoom_badge._minus, Qt.MouseButton.LeftButton)

        assert window._canvas.zoom == 80
        assert window._canvas.image_rect().width() < before

    def test_it_steps_by_the_amount_the_design_specifies(self):
        _low, _high, step = tokens.WinMetric.ZOOM_STEPS
        window = ReviewWindow(make_image())
        start = window._canvas.zoom

        QTest.mouseClick(window._zoom_badge._plus, Qt.MouseButton.LeftButton)

        assert window._canvas.zoom == start + step

    def test_it_clamps_at_both_ends_of_the_designed_range(self):
        low, high, _step = tokens.WinMetric.ZOOM_STEPS
        window = ReviewWindow(make_image())

        for _ in range(20):
            QTest.mouseClick(window._zoom_badge._minus, Qt.MouseButton.LeftButton)
        assert window._canvas.zoom == low

        for _ in range(40):
            QTest.mouseClick(window._zoom_badge._plus, Qt.MouseButton.LeftButton)
        assert window._canvas.zoom == high

    def test_the_percentage_follows_the_canvas(self):
        window = ReviewWindow(make_image())

        QTest.mouseClick(window._zoom_badge._plus, Qt.MouseButton.LeftButton)

        assert window._zoom_badge._percent.text() == f"{window._canvas.zoom}%"

    def test_each_end_dims_when_there_is_nowhere_further_to_go(self):
        low, high, _step = tokens.WinMetric.ZOOM_STEPS
        window = ReviewWindow(make_image())

        for _ in range(20):
            QTest.mouseClick(window._zoom_badge._minus, Qt.MouseButton.LeftButton)
        assert window._zoom_badge._minus._enabled_look is False
        assert window._zoom_badge._plus._enabled_look is True

        for _ in range(40):
            QTest.mouseClick(window._zoom_badge._plus, Qt.MouseButton.LeftButton)
        assert window._zoom_badge._plus._enabled_look is False
        assert window._zoom_badge._minus._enabled_look is True

    def test_zooming_repaints_the_canvas_differently(self):
        # The strongest form of the assertion: not "the number changed" but
        # "the pixels did". A canvas that tracked zoom without redrawing
        # would satisfy every test above and still look frozen.
        window = ReviewWindow(make_image(200, 150))
        window.resize(900, 700)
        before = window._canvas.grab().toImage()

        QTest.mouseClick(window._zoom_badge._plus, Qt.MouseButton.LeftButton)
        after = window._canvas.grab().toImage()

        assert before != after


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

    def test_its_bar_does_not_drag(self):
        # The overlay's bar can be dragged (#50), because over a selection
        # the size of a monitor it has nowhere else to go. This one is laid
        # out at the canvas floor, which the fitted image keeps a margin
        # from, and is never placed over a selection -- so it has nothing a
        # drag could be clamped to, and a press on its surface stays still.
        window = ReviewWindow(make_image())
        window._set_annotating(True)
        bar = window._bar
        bar.resize(bar.sizeHint())
        bar.layout().activate()
        before = bar.pos()
        grip = QPointF(bar.width() / 2, 2)
        assert bar.childAt(grip.toPoint()) is None, "a press there is the bar's own"

        QApplication.sendEvent(bar, _press(bar, grip.x(), grip.y()))
        QApplication.sendEvent(bar, _move(bar, grip.x(), grip.y() - 200))
        QApplication.sendEvent(bar, _release(bar, grip.x(), grip.y() - 200))

        assert bar.pos() == before
        assert not bar.is_dragging
        assert bar.cursor().shape() != Qt.CursorShape.OpenHandCursor

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


class TestTheStylePopoverIsReachable:
    """The other half of the report: the tools were selectable but not
    configurable -- no pen size, no brush size, no colour -- because what
    set those lived on the overlay window, and the review window had none.
    It gets the overlay's own style popover (#68), from the same style dot,
    over the same per-tool style.
    """

    def _editing(self) -> ReviewWindow:
        window = ReviewWindow(make_image(600, 400))
        window.resize(1020, 700)
        window._set_annotating(True)
        window._canvas.resize(1020, 600)
        return window

    @staticmethod
    def _drag(window: ReviewWindow, tool: str):
        window._bar.select_tool(tool)
        canvas = window._canvas
        canvas.mousePressEvent(_press(canvas, 400, 300))
        canvas.mouseMoveEvent(_move(canvas, 500, 380))
        canvas.mouseReleaseEvent(_release(canvas, 500, 380))
        return window._store.marks[-1]

    def _opened(self, tool: str) -> ReviewWindow:
        window = self._editing()
        window._bar.select_tool(tool)
        QTest.mouseClick(window._bar._style_dot, Qt.MouseButton.LeftButton)
        return window

    def test_it_stays_down_until_the_style_dot_opens_it(self):
        window = self._editing()

        window._bar.select_tool("pen")

        assert not window._style_popover.isVisibleTo(window._canvas)

    def test_the_style_dot_opens_it_for_the_active_tool(self):
        window = self._opened("pen")

        assert window._style_popover.isVisibleTo(window._canvas)
        assert window._style_popover.sections() == ["color", "size"]
        assert window._bar._style_dot.is_open

    def test_for_a_redaction_it_offers_the_strength_alone(self):
        window = self._opened("blur")

        assert window._style_popover.sections() == ["strength"]

    def test_the_eraser_does_not_open_it(self):
        window = self._opened("eraser")

        assert not window._style_popover.isVisibleTo(window._canvas)

    def test_it_opens_above_the_bar(self):
        window = ReviewWindow(make_image(900, 600))
        window.resize(1020, 700)
        window.show()
        window._set_annotating(True)
        window._bar.select_tool("rect")

        window._toggle_style()

        assert window._style_popover.isVisible()
        assert window._style_popover.geometry().bottom() < window._bar.geometry().top()

    def test_it_stays_open_across_picks_and_keys(self):
        window = self._opened("rect")
        popover = window._style_popover

        QTest.mouseClick(popover._swatch_buttons[tokens.INK_SWATCHES[2][1]], Qt.MouseButton.LeftButton)
        QTest.mouseClick(popover._fill_button, Qt.MouseButton.LeftButton)
        QTest.keyClick(window, Qt.Key.Key_D)

        assert popover.isVisibleTo(window._canvas)

    def test_the_stroke_slider_sets_the_next_marks_stroke(self):
        window = self._opened("pen")

        window._style_popover._size_slider.setValue(22)

        assert self._drag(window, "pen").stroke_width == 22

    def test_a_swatch_sets_the_next_marks_colour(self):
        window = self._opened("rect")
        _name, hex_colour = tokens.INK_SWATCHES[5]

        QTest.mouseClick(window._style_popover._swatch_buttons[hex_colour], Qt.MouseButton.LeftButton)

        assert self._drag(window, "rect").colour == QColor(hex_colour)

    def test_a_fill_and_line_pick_reach_the_next_mark(self):
        window = self._opened("ellipse")

        QTest.mouseClick(window._style_popover._fill_button, Qt.MouseButton.LeftButton)
        QTest.mouseClick(window._style_popover._dash_button, Qt.MouseButton.LeftButton)

        mark = self._drag(window, "ellipse")
        assert (mark.fill, mark.dash) == ("filled", "dashed")

    def test_a_strength_reaches_the_next_redaction(self):
        window = self._opened("pixelate")

        window._style_popover._strength_slider.setValue(14)

        assert self._drag(window, "pixelate").strength == 14

    def test_the_style_dot_previews_what_it_set(self):
        window = self._opened("pen")
        _name, red = tokens.INK_SWATCHES[1]

        QTest.mouseClick(window._style_popover._swatch_buttons[red], Qt.MouseButton.LeftButton)
        window._style_popover._size_slider.setValue(11)

        assert (window._bar._style_dot.style.colour, window._bar._style_dot.style.size) == (red, 11)

    def test_the_style_keys_work_while_editing(self):
        window = self._editing()
        window._bar.select_tool("arrow")
        seed = window._styles.of("arrow")

        QTest.keyClick(window, Qt.Key.Key_2)
        QTest.keyClick(window, Qt.Key.Key_BracketRight)
        QTest.keyClick(window, Qt.Key.Key_D)

        style = window._styles.of("arrow")
        assert (style.colour, style.size, style.dash) == (
            tokens.INK_SWATCHES[1][1], seed.size + 1, "dashed"
        )

    def test_the_style_keys_do_nothing_while_not_editing(self):
        window = ReviewWindow(make_image())
        window._bar.select_tool("pen")
        before = window._styles.of("pen")

        QTest.keyClick(window, Qt.Key.Key_2)

        assert window._styles.of("pen") == before

    def test_it_draws_with_the_style_the_overlay_left_for_the_session(self):
        session_styles.update("pen", colour="#123456", size=9)
        window = self._editing()

        mark = self._drag(window, "pen")

        assert (mark.colour, mark.stroke_width) == (QColor("#123456"), 9)

    def test_leaving_edit_mode_puts_it_away(self):
        window = self._opened("pen")

        window._set_annotating(False)

        assert not window._style_popover.isVisibleTo(window._canvas)
        assert not window._bar._style_dot.is_open


class TestEditLabel:
    def test_the_button_says_edit_not_annotate(self):
        assert ReviewWindow(make_image())._annotate_button.text() == "Edit"

    def test_and_done_editing_while_editing(self):
        window = ReviewWindow(make_image())

        window._set_annotating(True)

        assert window._annotate_button.text() == "Done editing"


class TestTextTool:
    """The text tool works here now: it drives the same `TextLabelEditor`
    the overlay does, with one difference -- the field is placed at the
    widget point clicked, but the mark it commits is stored in image
    coordinates like every other mark in this window.
    """

    def _editing(self) -> ReviewWindow:
        window = ReviewWindow(make_image(600, 400))
        window.resize(1020, 700)
        window._canvas.resize(1020, 600)
        window._set_annotating(True)
        window._canvas.set_tool("text")
        return window

    def test_clicking_opens_a_label_and_commits_no_mark_yet(self):
        window = self._editing()

        window._canvas.mousePressEvent(_press(window._canvas, 500, 300))

        assert window._canvas._text_editor.field is not None
        assert len(window._store) == 0

    def test_typing_then_finishing_commits_a_text_mark(self):
        window = self._editing()
        window._canvas.mousePressEvent(_press(window._canvas, 500, 300))

        window._canvas._text_editor.field.setText("hello")
        window._canvas._text_editor.commit()

        assert len(window._store) == 1
        assert window._store.marks[0].text == "hello"

    def test_the_mark_is_stored_in_image_coordinates(self):
        # The widget point clicked is not the image point stored -- if it
        # were, the label would export somewhere else entirely.
        window = self._editing()
        canvas = window._canvas

        canvas.mousePressEvent(_press(canvas, 500, 300))
        canvas._text_editor.field.setText("x")
        canvas._text_editor.commit()

        expected = canvas.to_image(QPointF(500, 300))
        assert window._store.marks[0].point == expected

    def test_an_empty_label_commits_nothing(self):
        window = self._editing()
        window._canvas.mousePressEvent(_press(window._canvas, 500, 300))

        window._canvas._text_editor.commit()

        assert len(window._store) == 0

    def test_a_second_click_commits_the_first_label(self):
        # A click elsewhere never blurs the field, so nothing else would
        # force the commit and the first label would be lost.
        window = self._editing()
        canvas = window._canvas
        canvas.mousePressEvent(_press(canvas, 400, 250))
        canvas._text_editor.field.setText("first")

        canvas.mousePressEvent(_press(canvas, 600, 350))

        assert len(window._store) == 1
        assert window._store.marks[0].text == "first"

    def test_switching_to_another_tool_commits_it_too(self):
        window = self._editing()
        canvas = window._canvas
        canvas.mousePressEvent(_press(canvas, 400, 250))
        canvas._text_editor.field.setText("kept")

        canvas.set_tool("pen")
        canvas.mousePressEvent(_press(canvas, 500, 300))

        assert any(getattr(m, "text", None) == "kept" for m in window._store.marks)

    def test_leaving_edit_mode_commits_it(self):
        window = self._editing()
        window._canvas.mousePressEvent(_press(window._canvas, 400, 250))
        window._canvas._text_editor.field.setText("saved on exit")

        window._set_annotating(False)

        assert len(window._store) == 1

    def test_abandon_drops_it_without_committing(self):
        window = self._editing()
        window._canvas.mousePressEvent(_press(window._canvas, 400, 250))
        window._canvas._text_editor.field.setText("discarded")

        window._canvas.abandon_text()

        assert len(window._store) == 0

    def test_the_text_mark_reaches_the_exported_image(self, tmp_path):
        window = self._editing()
        canvas = window._canvas
        canvas.mousePressEvent(_press(canvas, 500, 300))
        canvas._text_editor.field.setText("exported")
        canvas._text_editor.commit()

        target = window.save_as(tmp_path / "with-text.png")

        assert target.exists()
        assert any(getattr(m, "text", None) == "exported" for m in window._store.marks)


class TestThePopoverFollowsTheTool:
    def test_it_styles_whichever_tool_is_armed(self):
        # Left untold it would style whichever tool it last showed.
        window = ReviewWindow(make_image())
        window.resize(1020, 700)
        window._set_annotating(True)
        window._bar.select_tool("pen")
        window._toggle_style()

        QTest.keyClick(window, Qt.Key.Key_T)

        assert window._style_popover.tool == "text"
        assert window._style_popover.isVisibleTo(window._canvas)
        assert window._style_popover._size_slider.toolTip() == "Text size — [ ]"


class TestEveryToolIsNamedOnScreen:
    """The eraser had no on-screen name anywhere: the old settings tray
    named every other tool, but only appeared for tools with colour and
    stroke to set. Its glyph is not self-explanatory at 16px, so the one tool
    with nothing to configure was the one tool you could not identify.
    """

    def _editing(self) -> ReviewWindow:
        window = ReviewWindow(make_image())
        window.resize(1020, 700)
        window._set_annotating(True)
        return window

    def test_the_eraser_is_named(self):
        window = self._editing()

        window._bar.select_tool("eraser")
        window._sync_tool_hint()

        assert window._tool_hint.isVisibleTo(window._canvas)
        assert window._tool_hint._pill._text_label.text() == "Eraser"

    def test_the_eraser_says_what_it_does(self):
        window = self._editing()

        window._bar.select_tool("eraser")
        window._sync_tool_hint()

        assert window._tool_hint._hint.text() == tokens.TOOL_HINTS["eraser"]

    def test_a_draw_tool_is_named_too_until_the_style_popover_opens(self):
        # The strip names every tool, and gives way to the popover rather
        # than landing on it.
        window = self._editing()
        window._bar.select_tool("pen")

        assert window._tool_hint.isVisibleTo(window._canvas)
        assert window._tool_hint._pill._text_label.text() == "Pen"

        window._toggle_style()

        assert not window._tool_hint.isVisibleTo(window._canvas)
        assert window._style_popover.isVisibleTo(window._canvas)

        window._toggle_style()

        assert window._tool_hint.isVisibleTo(window._canvas)

    def test_leaving_edit_mode_puts_the_strip_away(self):
        window = self._editing()
        window._bar.select_tool("eraser")
        window._sync_tool_hint()

        window._set_annotating(False)

        assert not window._tool_hint.isVisibleTo(window._canvas)


class TestToolButtonsCarryTooltips:
    def test_every_tool_button_has_one(self):
        window = ReviewWindow(make_image())

        for tool, button in window._bar._tool_buttons.items():
            assert button.toolTip(), f"{tool} has no tooltip"

    def test_the_tooltip_names_the_tool_and_its_key(self):
        window = ReviewWindow(make_image())

        assert window._bar._tool_buttons["eraser"].toolTip() == "Eraser — E"

    def test_buttons_track_the_mouse_so_the_tooltip_timer_fires(self):
        # Qt's wake-up timer is driven by moves over the widget, not by the
        # enter event alone.
        window = ReviewWindow(make_image())

        assert window._bar._tool_buttons["eraser"].hasMouseTracking()


class TestEraserReachesEveryMark:
    """It looked like the eraser only worked on freehand strokes. Two
    causes: a mark's hit tolerance is fixed in image units, so at anything
    under 100% zoom it shrank on screen to a couple of pixels; and a box's
    empty middle deliberately hit nothing at all.
    """

    def _store_with(self, shape):
        from snipux.marks import MarkStore

        store = MarkStore()
        store.add(shape)
        return store

    def _two_point(self, tool):
        from snipux.marks import begin_stroke, extend_stroke

        shape = begin_stroke(
            tool, QPointF(100, 100), colour=QColor("#e3ff4f"), stroke_width=6
        )
        extend_stroke(shape, QPointF(300, 250))
        return shapes.finalize_mark(shape)

    @pytest.mark.parametrize("tool", ["pen", "highlighter", "arrow", "rect",
                                      "ellipse", "line", "blur"])
    def test_clicking_a_mark_erases_it(self, tool):
        shape = self._two_point(tool)
        store = self._store_with(shape)
        centre = QPointF(200, 175)

        assert store.erase(centre) is not None, f"{tool} could not be erased"

    def test_a_box_is_erased_by_clicking_inside_it(self):
        # The empty middle used to hit nothing, which is most of why the
        # eraser read as broken.
        store = self._store_with(self._two_point("rect"))

        assert store.erase(QPointF(200, 175)) is not None

    def test_but_a_mark_inside_the_box_still_wins(self):
        # Outlines are tested first, all of them, so clicking near the mark
        # inside a box takes the mark rather than the box.
        from snipux.marks import MarkStore

        store = MarkStore()
        store.add(self._two_point("rect"))
        inner = shapes.StepMarker(
            colour=QColor("#fff"), stroke_width=6, point=QPointF(200, 175), number=1
        )
        store.add(inner)

        assert store.erase(QPointF(200, 175)) is inner

    def test_slack_forgives_a_near_miss_on_a_thin_outline(self):
        # What the review window passes, scaled against the live zoom.
        store = self._store_with(self._two_point("line"))
        near_miss = QPointF(200, 175 + 9)

        assert store.erase(near_miss, slack=0.0) is None
        store.undo() if store.can_undo else None
        store = self._store_with(self._two_point("line"))
        assert store.erase(near_miss, slack=12.0) is not None


class TestFamilySlots:
    """The review window's bar is the overlay's, families and all. Picking a
    tool uses it: a click arms the sibling the slot shows, a click on the
    armed slot opens or closes its menu, and neither ever cycles.
    """

    def _editing(self) -> ReviewWindow:
        window = ReviewWindow(make_image())
        window.resize(1020, 700)
        window._set_annotating(True)
        return window

    def test_the_first_click_arms_the_current_shape(self):
        window = self._editing()

        QTest.mouseClick(window._bar._tool_buttons["shapes"], Qt.MouseButton.LeftButton)

        assert window._bar.active_tool == "rect"

    def test_clicking_the_armed_slot_opens_its_menu_and_keeps_the_shape(self):
        window = self._editing()
        button = window._bar._tool_buttons["shapes"]

        QTest.mouseClick(button, Qt.MouseButton.LeftButton)
        QTest.mouseClick(button, Qt.MouseButton.LeftButton)

        assert window._family_menus["shapes"].isVisibleTo(window._canvas)
        assert window._bar.active_tool == "rect"

    def test_clicking_it_once_more_closes_the_menu(self):
        window = self._editing()
        button = window._bar._tool_buttons["shapes"]

        for _ in range(3):
            QTest.mouseClick(button, Qt.MouseButton.LeftButton)

        assert not window._family_menus["shapes"].isVisibleTo(window._canvas)
        assert window._bar.active_tool == "rect"

    def test_the_notch_opens_the_family_menu(self):
        window = self._editing()
        notch = window._bar._tool_buttons["redact"].notch

        QTest.mouseClick(notch, Qt.MouseButton.LeftButton)

        assert window._family_menus["redact"].isVisibleTo(window._canvas)
        assert window._bar.active_tool != "blur", "the notch opens, it does not arm"

    def test_a_row_arms_its_sibling_and_the_slot_shows_it(self):
        window = self._editing()
        QTest.mouseClick(window._bar._tool_buttons["shapes"].notch, Qt.MouseButton.LeftButton)
        menu = window._family_menus["shapes"]

        QTest.mouseClick(menu._rows["ellipse"], Qt.MouseButton.LeftButton)

        assert window._bar.active_tool == "ellipse"
        assert window._canvas._tool == "ellipse"
        assert window._bar._tool_buttons["shapes"]._icon_name == "ellipse"
        assert not menu.isVisibleTo(window._canvas)

    def test_choosing_from_the_menu_also_moves_the_glyph(self):
        window = ReviewWindow(make_image())

        window._bar.select_tool("line")

        assert window._bar._tool_buttons["shapes"]._icon_name == "line"

    def test_a_press_on_the_image_closes_the_menu_and_draws_nothing(self):
        window = self._editing()
        window._bar.select_tool("pen")
        QTest.mouseClick(window._bar._tool_buttons["shapes"].notch, Qt.MouseButton.LeftButton)
        canvas = window._canvas
        canvas.resize(1020, 600)

        QApplication.sendEvent(canvas, _press(canvas, 400, 300))

        assert not window._family_menus["shapes"].isVisibleTo(canvas)
        assert canvas._in_progress is None

    @pytest.mark.parametrize(
        "key,tool",
        [
            (Qt.Key.Key_R, "rect"),
            (Qt.Key.Key_O, "ellipse"),
            (Qt.Key.Key_L, "line"),
            (Qt.Key.Key_A, "arrow"),
            (Qt.Key.Key_E, "eraser"),
        ],
    )
    def test_each_key_reaches_its_tool_while_editing(self, key, tool):
        window = self._editing()

        QTest.keyClick(window, key)

        assert window._bar.active_tool == tool

    def test_b_cycles_the_redaction_family(self):
        window = self._editing()

        seen = []
        for _ in range(4):
            QTest.keyClick(window, Qt.Key.Key_B)
            seen.append(window._bar.active_tool)

        assert seen == ["blur", "pixelate", "blackout", "blur"]

    def test_the_keys_do_nothing_while_not_editing(self):
        window = ReviewWindow(make_image())

        QTest.keyClick(window, Qt.Key.Key_R)

        assert window._bar.active_tool is None


class TestHoverNamesTheTool:
    def test_hovering_names_the_tool_without_arming_it(self):
        window = ReviewWindow(make_image())
        window.resize(1020, 700)
        window._set_annotating(True)
        window._bar.select_tool("pen")
        window._sync_tool_hint()

        window._bar._tool_buttons["eraser"].hovered.emit("eraser")

        assert window._tool_hint._pill._text_label.text() == "Eraser"
        assert window._bar.active_tool == "pen", "hovering must not arm anything"

    def test_hovering_leaves_an_open_style_popover_alone(self):
        # The strip would land on the popover, which is what is being used.
        window = ReviewWindow(make_image())
        window.resize(1020, 700)
        window._set_annotating(True)
        window._bar.select_tool("pen")
        window._toggle_style()

        window._bar._tool_buttons["eraser"].hovered.emit("eraser")
        on_hover = window._tool_hint.isVisibleTo(window._canvas)
        window._bar._tool_buttons["eraser"].unhovered.emit()

        assert not on_hover
        assert window._style_popover.isVisibleTo(window._canvas)


class TestCopyConfirms:
    def test_copy_shows_a_toast(self):
        # A word changing in the footer is not where anyone is looking when
        # they press the button they just pressed.
        window = ReviewWindow(make_image(64, 64))
        window.resize(1020, 700)

        window.copy()

        assert window._toast._text_label.text() == "Copied to clipboard"

    def test_save_as_shows_one_too(self, tmp_path):
        window = ReviewWindow(make_image(64, 64))
        window.resize(1020, 700)

        window.save_as(tmp_path / "shot.png")

        assert "Saved to" in window._toast._text_label.text()


class TestLabelsAreClickableAndNotBlackBoxes:
    """Two faults in one feature. `Text.draw` puts the chip's top-left at
    the anchor while `hit_test` centred a fixed box *on* it, so the label
    you could see was not the label you could click; and the live field had
    no stylesheet at all, so a QLineEdit painted its palette's opaque base
    -- a black rectangle over the screenshot.
    """

    def _label(self, text="asdasdas"):
        return shapes.Text(
            colour=QColor("#e3ff4f"), stroke_width=6,
            point=QPointF(100, 100), text=text,
        )

    def _store_with(self, shape):
        from snipux.marks import MarkStore

        store = MarkStore()
        store.add(shape)
        return store

    def test_the_hit_region_is_the_chip_that_is_drawn(self):
        label = self._label()

        assert label.hit_test(label.chip_rect().center())

    def test_clicking_the_far_end_of_a_label_erases_it(self):
        # The end furthest from the anchor is exactly what the old centred
        # box missed.
        label = self._label("a much longer label than before")
        store = self._store_with(label)
        far_end = QPointF(label.chip_rect().right() - 4, label.chip_rect().center().y())

        assert store.erase(far_end) is label

    def test_a_longer_label_has_a_wider_hit_region(self):
        assert (
            self._label("a much longer label").chip_rect().width()
            > self._label("x").chip_rect().width()
        )

    def test_the_live_field_is_styled_so_it_is_not_an_opaque_box(self):
        window = ReviewWindow(make_image())
        window.resize(1020, 700)
        window._set_annotating(True)

        window._canvas._text_editor.begin(QPointF(200, 200), QColor("#e3ff4f"), 6)

        style = window._canvas._text_editor.field.styleSheet()
        assert "background: rgba(" in style, "an unstyled QLineEdit paints an opaque box"
        assert "border-radius" in style


class TestEraserSweeps:
    """Rubbing something out is a sweep everywhere else it exists; making
    the user aim at each mark in turn was the odd one out.
    """

    def _editing(self):
        window = ReviewWindow(make_image(600, 400))
        window.resize(1020, 700)
        window._canvas.resize(1020, 600)
        window._set_annotating(True)
        window._canvas.set_tool("eraser")
        return window

    def _pen_at(self, x, y):
        # Two points, not one: a single-point polyline has no path, so it
        # would hit-test as nothing and the test would pass for the wrong
        # reason.
        return shapes.Pen(
            colour=QColor("#fff"), stroke_width=8,
            points=[QPointF(x - 6, y), QPointF(x + 6, y)],
        )

    def test_a_drag_erases_everything_it_passes_over(self):
        window = self._editing()
        canvas = window._canvas
        for x in (100, 150, 200):
            window._store.add(self._pen_at(x, 200))
        assert len(window._store) == 3

        start = canvas.image_rect().topLeft()
        scale = canvas._scale()
        canvas.mousePressEvent(
            _press(canvas, start.x() + 100 * scale, start.y() + 200 * scale)
        )
        for x in (120, 150, 180, 200):
            canvas.mouseMoveEvent(
                _move(canvas, start.x() + x * scale, start.y() + 200 * scale)
            )
        canvas.mouseReleaseEvent(
            _release(canvas, start.x() + 200 * scale, start.y() + 200 * scale)
        )

        assert len(window._store) == 0, "a sweep should take all three"

    def test_the_sweep_stops_at_release(self):
        window = self._editing()
        canvas = window._canvas
        window._store.add(self._pen_at(100, 200))
        start = canvas.image_rect().topLeft()
        scale = canvas._scale()
        canvas.mousePressEvent(_press(canvas, start.x() + 400 * scale, start.y() + 50 * scale))
        canvas.mouseReleaseEvent(_release(canvas, start.x() + 400 * scale, start.y() + 50 * scale))

        # Moving after release must not keep erasing.
        canvas.mouseMoveEvent(_move(canvas, start.x() + 100 * scale, start.y() + 200 * scale))

        assert len(window._store) == 1


class TestToastClearsTheBar:
    """Both wanted bottom centre, so the toast landed underneath the
    floating bar and showed as a dark sliver poking out from it -- which
    reads as a rendering fault rather than a message.
    """

    def _window(self):
        window = ReviewWindow(make_image(900, 600))
        window.resize(1020, 700)
        window.show()
        return window

    def test_with_no_bar_it_uses_the_canvas_floor(self):
        window = self._window()

        window.copy()

        assert window._toast.geometry().bottom() > window._canvas.height() - 80

    def test_while_editing_it_sits_clear_of_the_bar(self):
        window = self._window()
        window._set_annotating(True)

        window.copy()

        assert not window._toast.geometry().intersects(window._bar.geometry())

    def test_it_also_clears_an_open_style_popover(self):
        window = self._window()
        window._set_annotating(True)
        window._bar.select_tool("pen")
        window._toggle_style()
        popover = window._style_popover.geometry()
        assert window._style_popover.isVisible()

        window.copy()

        assert not window._toast.geometry().intersects(popover)
        assert window._toast.geometry().bottom() <= popover.top()


def _hover(widget, x, y):
    """A move with no button held -- what arrives after a release was lost."""
    return QMouseEvent(
        QEvent.Type.MouseMove, QPointF(x, y), QPointF(x, y),
        Qt.MouseButton.NoButton, Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
    )


class TestADragEndsWithoutItsRelease:
    """The review canvas keeps the overlay's guards: a release that never
    arrives must not leave a mark stretching after the pointer.
    """

    def _dragging(self, tool: str = "rect") -> ReviewWindow:
        window = ReviewWindow(make_image(600, 400))
        window.resize(1020, 700)
        window._set_annotating(True)
        window._canvas.resize(1020, 600)
        window._bar.select_tool(tool)
        canvas = window._canvas
        canvas.mousePressEvent(_press(canvas, 400, 300))
        canvas.mouseMoveEvent(_move(canvas, 500, 380))
        return window

    def test_a_move_with_no_button_held_commits_the_mark(self):
        window = self._dragging()
        canvas = window._canvas

        canvas.mouseMoveEvent(_hover(canvas, 700, 500))

        assert canvas._in_progress is None
        assert len(window._store) == 1
        # As the drag stood at its last move, not where the pointer went on to.
        assert window._store.marks[0].end == canvas.to_image(QPointF(500, 380))

    def test_later_movement_no_longer_stretches_it(self):
        window = self._dragging()
        canvas = window._canvas
        canvas.mouseMoveEvent(_hover(canvas, 700, 500))

        canvas.mouseMoveEvent(_move(canvas, 900, 580))

        assert window._store.marks[0].end == canvas.to_image(QPointF(500, 380))

    def test_a_new_press_commits_the_open_drag_first(self):
        window = self._dragging()
        canvas = window._canvas

        canvas.mousePressEvent(_press(canvas, 200, 150))

        assert len(window._store) == 1
        assert window._store.marks[0].end == canvas.to_image(QPointF(500, 380))

    def test_focus_leaving_the_window_commits_it(self):
        window = self._dragging()
        canvas = window._canvas

        QApplication.sendEvent(canvas, QEvent(QEvent.Type.WindowDeactivate))

        assert canvas._in_progress is None
        assert len(window._store) == 1


class TestBlackoutExportsItsFill:
    def test_the_blacked_out_pixels_are_the_blackout_fill(self):
        window = ReviewWindow(make_image(600, 400))
        window.resize(1020, 700)
        window._set_annotating(True)
        canvas = window._canvas
        canvas.resize(1020, 600)
        window._bar.select_tool("blackout")

        canvas.mousePressEvent(_press(canvas, 400, 300))
        canvas.mouseMoveEvent(_move(canvas, 500, 380))
        canvas.mouseReleaseEvent(_release(canvas, 500, 380))
        inside = canvas.to_image(QPointF(450, 340)).toPoint()

        rendered = window._canvas.rendered_image()

        assert type(window._store.marks[0]) is shapes.Blackout
        assert rendered.pixelColor(inside) == QColor(tokens.BLACKOUT_FILL)


class TestNoSecondWatermark:
    """#69: the review window's bar has no watermark slot, and its exports
    stamp nothing. The snip it opens was exported by the overlay, stamped
    already if the watermark was on, so a second stamp could only double it.
    """

    def test_its_bar_has_no_watermark_slot(self):
        window = ReviewWindow(make_image())

        window._set_annotating(True)

        assert window._bar._watermark.isHidden()

    def test_its_export_is_not_stamped_even_with_the_watermark_on(self):
        from snipux import overlay, setup_desktop

        setup_desktop.save_watermark_text("acme")
        overlay.watermark_session.enabled = True
        window = ReviewWindow(make_image())

        assert window._canvas.rendered_image() == make_image()


class TestTheHighlighterSnapsToTextHere:
    """The review window snaps a highlighter sweep the way the overlay does,
    against the snip itself -- its marks are already in the image's pixels."""

    @staticmethod
    def _text_image() -> QImage:
        image = QImage(600, 400, QImage.Format.Format_RGB32)
        image.fill(QColor("#ffffff"))
        painter = QPainter(image)
        font = QFont()
        font.setPixelSize(18)
        painter.setFont(font)
        painter.setPen(QColor("#202020"))
        painter.drawText(QPointF(40, 200), "several words of text")
        painter.end()
        return image

    def test_a_sweep_over_text_commits_as_a_band_on_the_line(self):
        window = ReviewWindow(self._text_image())
        window.resize(1020, 700)
        window._set_annotating(True)
        canvas = window._canvas
        canvas.resize(1020, 600)
        window._bar.select_tool("highlighter")
        rect = canvas.image_rect()
        scale = rect.width() / 600

        def on_screen(x, y):
            return rect.x() + x * scale, rect.y() + y * scale

        canvas.mousePressEvent(_press(canvas, *on_screen(60, 197)))
        canvas.mouseMoveEvent(_move(canvas, *on_screen(110, 190)))
        canvas.mouseMoveEvent(_move(canvas, *on_screen(160, 196)))
        canvas.mouseReleaseEvent(_release(canvas, *on_screen(160, 196)))

        (mark,) = window._store.marks
        (band,) = mark.bands
        # Image pixels: the line of text sits at roughly y=186 to y=205.
        assert 178 <= band.top() <= 190 and 200 <= band.bottom() <= 212
        assert band.left() <= 42
