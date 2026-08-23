import pytest
from PyQt6.QtCore import QPoint, QPointF, QSizeF, Qt
from PyQt6.QtGui import QColor, QImage, qRgb
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication

from snipux import editor as editor_module
from snipux.capture import Frame
from snipux.editor import Canvas, Editor, Tool
from snipux.shapes import Blur, Pixelate, Rectangle, StepMarker, Text

FILL_COLOR = qRgb(10, 20, 30)


@pytest.fixture(scope="module", autouse=True)
def qapp():
    # PyQt6 needs a live QApplication to construct any QWidget, even
    # offscreen. Module-scoped so every test in this file shares one.
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def make_frame(image_size=(100, 60), fill_color=FILL_COLOR) -> Frame:
    # scale 1.0 (logical_size == image_size in pixels) so widget-local
    # coordinates line up with image pixels 1:1, including for the crop
    # tests, which read logical_origin/logical_size through apply_crop().
    image = QImage(*image_size, QImage.Format.Format_RGB32)
    image.fill(fill_color)
    return Frame(
        image=image,
        logical_origin=QPointF(0, 0),
        logical_size=QSizeF(*image_size),
    )


class TestLetterbox:
    def test_centered_and_letterboxed_when_widget_larger_than_image(self):
        frame = make_frame(image_size=(100, 60))
        canvas = Canvas(frame)
        canvas.resize(200, 200)

        rendered = canvas.grab().toImage()

        target = canvas._target_rect()
        assert target.width() == 100
        assert target.height() == 60
        # Equal margins on both sides of the shorter axis confirms centering.
        assert target.left() == pytest.approx(50, abs=1)
        assert target.top() == pytest.approx(70, abs=1)

        center = target.center()
        assert rendered.pixelColor(round(center.x()), round(center.y())) == QColor(FILL_COLOR)
        # A widget corner is well outside the target rect for this geometry,
        # so it must be showing the letterbox fill, not the image.
        assert rendered.pixelColor(2, 2) != QColor(FILL_COLOR)


class TestScaling:
    def test_scales_down_preserving_aspect_when_widget_smaller_than_image(self):
        frame = make_frame(image_size=(400, 200))
        canvas = Canvas(frame)
        canvas.resize(100, 100)

        target = canvas._target_rect()

        assert target.width() <= 100
        assert target.height() <= 100
        assert target.width() / target.height() == pytest.approx(400 / 200, rel=1e-6)
        assert target.width() < 400  # actually scaled down, not clipped

    def test_never_upscales_past_100_percent(self):
        frame = make_frame(image_size=(100, 60))
        canvas = Canvas(frame)
        canvas.resize(500, 500)

        target = canvas._target_rect()

        # Scale pinned at 1.0: target size equals the image's own size,
        # never grown to fill the larger widget.
        assert target.width() == 100
        assert target.height() == 60


class TestRoundTrip:
    @pytest.mark.parametrize("widget_size", [(500, 500), (100, 100)])
    def test_round_trips_within_one_pixel(self, widget_size):
        frame = make_frame(image_size=(400, 200))
        canvas = Canvas(frame)
        canvas.resize(*widget_size)

        width, height = 400, 200
        samples = [
            QPointF(0, 0),
            QPointF(width - 1, height - 1),
            QPointF(width / 2, height / 2),
        ]

        for image_point in samples:
            widget_point = canvas.image_to_widget(image_point)
            round_tripped = canvas.widget_to_image(widget_point)
            assert round_tripped is not None
            assert abs(round_tripped.x() - image_point.x()) <= 1
            assert abs(round_tripped.y() - image_point.y()) <= 1


class TestMargin:
    def test_widget_to_image_returns_none_in_letterbox_margin(self):
        frame = make_frame(image_size=(100, 60))
        canvas = Canvas(frame)
        canvas.resize(200, 200)

        # The image is centered well away from the widget's own corner at
        # this geometry, so (0, 0) is squarely in the margin.
        assert canvas.widget_to_image(QPointF(0, 0)) is None


class TestGrabPixelMatch:
    def test_grab_pixels_match_source_image(self):
        frame = make_frame(image_size=(100, 60), fill_color=qRgb(200, 50, 90))
        canvas = Canvas(frame)
        canvas.resize(100, 60)  # scale == 1.0: no interpolation to fuzz the comparison

        rendered = canvas.grab().toImage()

        image_point = QPointF(40, 20)
        widget_point = canvas.image_to_widget(image_point)
        sampled = rendered.pixelColor(round(widget_point.x()), round(widget_point.y()))
        expected = canvas.image.pixelColor(round(image_point.x()), round(image_point.y()))
        assert sampled == expected


class TestDegenerateImage:
    def test_zero_size_image_does_not_raise(self):
        frame = make_frame(image_size=(0, 0))
        canvas = Canvas(frame)
        canvas.resize(200, 200)

        assert canvas._target_rect().width() == 0
        assert canvas._target_rect().height() == 0
        assert canvas.widget_to_image(QPointF(10, 10)) is None
        # The case PLAN-REVIEW.md's first finding caught: this must degrade
        # gracefully (no ZeroDivisionError), not just return *some* value.
        result = canvas.image_to_widget(QPointF(0, 0))
        assert result == canvas._target_rect().topLeft()


class TestDragAppendsShape:
    def test_drag_with_tool_selected_appends_a_shape_of_that_type(self):
        frame = make_frame(image_size=(100, 60))
        canvas = Canvas(frame)
        canvas.resize(100, 60)  # scale == 1.0: widget-local == image-pixel
        canvas.set_tool(Tool.RECTANGLE)
        canvas.set_colour(QColor("red"))
        canvas.set_stroke_width(4)

        start = QPoint(10, 10)
        end = QPoint(60, 40)
        QTest.mousePress(canvas, Qt.MouseButton.LeftButton, pos=start)
        QTest.mouseMove(canvas, end)
        QTest.mouseRelease(canvas, Qt.MouseButton.LeftButton, pos=end)

        assert len(canvas.shapes) == 1
        assert isinstance(canvas.shapes[0], Rectangle)

    def test_no_tool_selected_is_a_no_op(self):
        frame = make_frame(image_size=(100, 60))
        canvas = Canvas(frame)
        canvas.resize(100, 60)
        # set_tool never called: canvas starts with no tool armed.

        start = QPoint(10, 10)
        end = QPoint(60, 40)
        QTest.mousePress(canvas, Qt.MouseButton.LeftButton, pos=start)
        QTest.mouseMove(canvas, end)
        QTest.mouseRelease(canvas, Qt.MouseButton.LeftButton, pos=end)

        assert len(canvas.shapes) == 0

    def test_press_in_letterbox_margin_is_a_no_op(self):
        frame = make_frame(image_size=(100, 60))
        canvas = Canvas(frame)
        canvas.resize(200, 200)  # image letterboxed; (5, 5) is margin, not image
        canvas.set_tool(Tool.RECTANGLE)

        # QPoint(0, 0) is QTest's own sentinel for "unspecified pos" (it
        # falls back to the widget's center), so this uses (5, 5) instead
        # to land in the margin unambiguously.
        QTest.mousePress(canvas, Qt.MouseButton.LeftButton, pos=QPoint(5, 5))
        QTest.mouseMove(canvas, QPoint(8, 8))
        QTest.mouseRelease(canvas, Qt.MouseButton.LeftButton, pos=QPoint(8, 8))

        assert len(canvas.shapes) == 0


class TestToolbarControls:
    def test_tool_actions_cover_every_tool_and_switching_updates_canvas(self):
        frame = make_frame()
        editor = Editor(frame)

        assert set(editor.tool_actions) == set(Tool)
        assert editor.canvas._tool is not None  # a usable default is armed

        editor.tool_actions[Tool.RECTANGLE].trigger()

        assert editor.canvas._tool is Tool.RECTANGLE
        assert editor.tool_actions[Tool.RECTANGLE].isChecked()

    def test_colour_swatch_click_sets_canvas_colour_with_no_dialog(self):
        frame = make_frame()
        editor = Editor(frame)
        red = QColor("red")

        editor.colour_buttons[red.name()].click()

        assert editor.canvas._colour == red

    def test_stroke_width_spinbox_updates_canvas(self):
        frame = make_frame()
        editor = Editor(frame)

        editor.stroke_width_spinbox.setValue(9)

        assert editor.canvas._stroke_width == 9


class TestEditorGrabRectangleBorder:
    def test_rectangle_border_shows_non_background_pixels(self):
        frame = make_frame(image_size=(100, 60), fill_color=FILL_COLOR)
        editor = Editor(frame)
        editor.resize(300, 300)
        editor.canvas.set_tool(Tool.RECTANGLE)
        editor.canvas.set_colour(QColor(255, 0, 0))
        editor.canvas.set_stroke_width(4)

        start_image = QPointF(10, 10)
        end_image = QPointF(80, 40)
        start_widget = editor.canvas.image_to_widget(start_image).toPoint()
        end_widget = editor.canvas.image_to_widget(end_image).toPoint()

        QTest.mousePress(editor.canvas, Qt.MouseButton.LeftButton, pos=start_widget)
        QTest.mouseMove(editor.canvas, end_widget)
        QTest.mouseRelease(editor.canvas, Qt.MouseButton.LeftButton, pos=end_widget)

        assert len(editor.canvas.shapes) == 1

        # Border point in canvas-local coordinates, translated into
        # Editor-local coordinates via canvas.pos() — Editor.grab() samples
        # Editor's own widget, and the toolbar offsets the canvas within it,
        # so reusing canvas-only coordinates against an editor grab would
        # sample the wrong pixels (letterbox or toolbar, not the border).
        border_canvas_local = editor.canvas.image_to_widget(QPointF(10, 25))
        border_editor_local = border_canvas_local + QPointF(editor.canvas.pos())

        rendered = editor.grab().toImage()
        sampled = rendered.pixelColor(
            round(border_editor_local.x()), round(border_editor_local.y())
        )

        assert sampled != QColor(FILL_COLOR)


class TestStepMarkerClickCommit:
    def test_single_click_appends_one_step_marker(self):
        frame = make_frame(image_size=(100, 60))
        canvas = Canvas(frame)
        canvas.resize(100, 60)  # scale == 1.0: widget-local == image-pixel
        canvas.set_tool(Tool.STEP_MARKER)

        pos = QPoint(20, 20)
        QTest.mousePress(canvas, Qt.MouseButton.LeftButton, pos=pos)
        QTest.mouseRelease(canvas, Qt.MouseButton.LeftButton, pos=pos)

        assert len(canvas.shapes) == 1
        assert isinstance(canvas.shapes[0], StepMarker)

    def test_a_drag_after_the_click_does_not_add_a_second_shape(self):
        # STEP_MARKER commits on press and never sets _in_progress_shape, so
        # a subsequent move/release from the same gesture must be a no-op —
        # the existing mouseMoveEvent/mouseReleaseEvent guards on
        # _in_progress_shape being None already cover this without change.
        frame = make_frame(image_size=(100, 60))
        canvas = Canvas(frame)
        canvas.resize(100, 60)
        canvas.set_tool(Tool.STEP_MARKER)

        start = QPoint(20, 20)
        end = QPoint(60, 40)
        QTest.mousePress(canvas, Qt.MouseButton.LeftButton, pos=start)
        QTest.mouseMove(canvas, end)
        QTest.mouseRelease(canvas, Qt.MouseButton.LeftButton, pos=end)

        assert len(canvas.shapes) == 1


class TestTextPlacement:
    # canvas.show() (and QApplication.processEvents() after each focus
    # change) is needed here, unlike the drag-based tool tests above: Qt
    # only honours setFocus()/clearFocus() on a widget whose ancestor chain
    # is actually shown, and this suite's editingFinished-based commit path
    # depends on real focus changes, not just synthesized key events.

    def test_click_type_and_return_commits_one_text_shape(self):
        frame = make_frame(image_size=(100, 60))
        canvas = Canvas(frame)
        canvas.resize(100, 60)
        canvas.show()
        canvas.set_tool(Tool.TEXT)

        QTest.mousePress(canvas, Qt.MouseButton.LeftButton, pos=QPoint(10, 10))
        QApplication.processEvents()
        assert canvas._text_edit is not None
        assert not canvas._text_edit.isHidden()

        QTest.keyClicks(canvas._text_edit, "hello")
        QTest.keyClick(canvas._text_edit, Qt.Key.Key_Return)

        assert len(canvas.shapes) == 1
        shape = canvas.shapes[0]
        assert isinstance(shape, Text)
        assert shape.text == "hello"
        assert canvas._text_edit.isHidden()

    def test_return_commits_exactly_once(self):
        # Regression test: editingFinished fires once for Enter and again
        # when _commit_text's own hide() call drops the field's focus.
        # Without the re-entrancy guard, a single Enter press would append
        # the same Text shape twice.
        frame = make_frame(image_size=(100, 60))
        canvas = Canvas(frame)
        canvas.resize(100, 60)
        canvas.show()
        canvas.set_tool(Tool.TEXT)

        QTest.mousePress(canvas, Qt.MouseButton.LeftButton, pos=QPoint(10, 10))
        QApplication.processEvents()
        QTest.keyClicks(canvas._text_edit, "hi")
        QTest.keyClick(canvas._text_edit, Qt.Key.Key_Return)

        assert len(canvas.shapes) == 1

    def test_empty_text_produces_no_shape(self):
        frame = make_frame(image_size=(100, 60))
        canvas = Canvas(frame)
        canvas.resize(100, 60)
        canvas.show()
        canvas.set_tool(Tool.TEXT)

        QTest.mousePress(canvas, Qt.MouseButton.LeftButton, pos=QPoint(10, 10))
        QApplication.processEvents()
        QTest.keyClick(canvas._text_edit, Qt.Key.Key_Return)

        assert len(canvas.shapes) == 0

    def test_focus_loss_commits_text(self):
        frame = make_frame(image_size=(100, 60))
        canvas = Canvas(frame)
        canvas.resize(100, 60)
        canvas.show()
        canvas.set_tool(Tool.TEXT)

        QTest.mousePress(canvas, Qt.MouseButton.LeftButton, pos=QPoint(10, 10))
        QApplication.processEvents()
        QTest.keyClicks(canvas._text_edit, "bye")
        canvas._text_edit.clearFocus()  # simulates "click elsewhere"
        QApplication.processEvents()

        assert len(canvas.shapes) == 1
        assert canvas.shapes[0].text == "bye"


class TestBlurPixelateDrag:
    def test_blur_tool_drag_appends_a_blur_shape(self):
        frame = make_frame(image_size=(100, 60))
        canvas = Canvas(frame)
        canvas.resize(100, 60)  # scale == 1.0: widget-local == image-pixel
        canvas.set_tool(Tool.BLUR)

        start = QPoint(10, 10)
        end = QPoint(60, 40)
        QTest.mousePress(canvas, Qt.MouseButton.LeftButton, pos=start)
        QTest.mouseMove(canvas, end)
        QTest.mouseRelease(canvas, Qt.MouseButton.LeftButton, pos=end)

        assert len(canvas.shapes) == 1
        assert isinstance(canvas.shapes[0], Blur)

    def test_pixelate_tool_drag_appends_a_pixelate_shape(self):
        frame = make_frame(image_size=(100, 60))
        canvas = Canvas(frame)
        canvas.resize(100, 60)
        canvas.set_tool(Tool.PIXELATE)

        start = QPoint(10, 10)
        end = QPoint(60, 40)
        QTest.mousePress(canvas, Qt.MouseButton.LeftButton, pos=start)
        QTest.mouseMove(canvas, end)
        QTest.mouseRelease(canvas, Qt.MouseButton.LeftButton, pos=end)

        assert len(canvas.shapes) == 1
        assert isinstance(canvas.shapes[0], Pixelate)

    def test_blur_drag_survives_repeated_paint_events_mid_drag(self):
        # paintEvent renders self._visible_shapes() (confirmed/in-progress)
        # on every call, which for BLUR means render() applies a growing,
        # possibly-degenerate Blur on every mouseMoveEvent — this must not
        # raise.
        frame = make_frame(image_size=(100, 60))
        canvas = Canvas(frame)
        canvas.resize(100, 60)
        canvas.show()
        canvas.set_tool(Tool.BLUR)

        QTest.mousePress(canvas, Qt.MouseButton.LeftButton, pos=QPoint(10, 10))
        QTest.mouseMove(canvas, QPoint(10, 10))  # degenerate: no movement yet
        QApplication.processEvents()
        QTest.mouseMove(canvas, QPoint(50, 40))
        QApplication.processEvents()
        QTest.mouseRelease(canvas, Qt.MouseButton.LeftButton, pos=QPoint(50, 40))

        assert len(canvas.shapes) == 1


class TestCropDrag:
    def test_crop_drag_replaces_the_frame_and_clears_shapes(self):
        frame = make_frame(image_size=(100, 60))
        canvas = Canvas(frame)
        canvas.resize(100, 60)  # scale == 1.0: widget-local == image-pixel
        # An existing annotation should be baked into the cropped image,
        # not discarded — see apply_crop()'s docstring.
        canvas.set_tool(Tool.RECTANGLE)
        QTest.mousePress(canvas, Qt.MouseButton.LeftButton, pos=QPoint(5, 5))
        QTest.mouseMove(canvas, QPoint(15, 15))
        QTest.mouseRelease(canvas, Qt.MouseButton.LeftButton, pos=QPoint(15, 15))
        assert len(canvas.shapes) == 1

        canvas.set_tool(Tool.CROP)
        start = QPoint(10, 10)
        end = QPoint(60, 40)
        QTest.mousePress(canvas, Qt.MouseButton.LeftButton, pos=start)
        QTest.mouseMove(canvas, end)
        QTest.mouseRelease(canvas, Qt.MouseButton.LeftButton, pos=end)

        assert canvas.image.width() == 50
        assert canvas.image.height() == 30
        # Baked into the new base image, not carried forward as a shape.
        assert len(canvas.shapes) == 0

    def test_zero_area_crop_drag_is_a_no_op(self):
        frame = make_frame(image_size=(100, 60))
        canvas = Canvas(frame)
        canvas.resize(100, 60)
        canvas.set_tool(Tool.CROP)

        pos = QPoint(20, 20)
        QTest.mousePress(canvas, Qt.MouseButton.LeftButton, pos=pos)
        QTest.mouseRelease(canvas, Qt.MouseButton.LeftButton, pos=pos)

        assert canvas.image.width() == 100
        assert canvas.image.height() == 60
        assert len(canvas.shapes) == 0


def _drag_rectangle(canvas: Canvas, start: QPoint, end: QPoint) -> None:
    canvas.set_tool(Tool.RECTANGLE)
    QTest.mousePress(canvas, Qt.MouseButton.LeftButton, pos=start)
    QTest.mouseMove(canvas, end)
    QTest.mouseRelease(canvas, Qt.MouseButton.LeftButton, pos=end)


class TestUndoRedo:
    def test_undo_after_three_shapes_removes_only_the_most_recent(self):
        frame = make_frame(image_size=(100, 60))
        canvas = Canvas(frame)
        canvas.resize(100, 60)  # scale == 1.0: widget-local == image-pixel

        _drag_rectangle(canvas, QPoint(5, 5), QPoint(15, 15))
        _drag_rectangle(canvas, QPoint(20, 20), QPoint(30, 30))
        _drag_rectangle(canvas, QPoint(35, 35), QPoint(45, 45))
        assert len(canvas.shapes) == 3
        first_two = canvas.shapes[:2]

        canvas.undo()

        assert len(canvas.shapes) == 2
        assert canvas.shapes == first_two

    def test_redo_after_undo_restores_the_exact_shape_removed(self):
        frame = make_frame(image_size=(100, 60))
        canvas = Canvas(frame)
        canvas.resize(100, 60)

        _drag_rectangle(canvas, QPoint(5, 5), QPoint(15, 15))
        _drag_rectangle(canvas, QPoint(20, 20), QPoint(30, 30))
        _drag_rectangle(canvas, QPoint(35, 35), QPoint(45, 45))
        all_three = canvas.shapes
        removed = all_three[2]

        canvas.undo()
        canvas.redo()

        assert canvas.shapes == all_three
        assert canvas.shapes[2] is removed

    def test_new_shape_after_undo_discards_the_stale_redo_entry(self):
        frame = make_frame(image_size=(100, 60))
        canvas = Canvas(frame)
        canvas.resize(100, 60)

        _drag_rectangle(canvas, QPoint(5, 5), QPoint(15, 15))
        _drag_rectangle(canvas, QPoint(20, 20), QPoint(30, 30))
        canvas.undo()
        assert len(canvas.shapes) == 1

        _drag_rectangle(canvas, QPoint(50, 5), QPoint(55, 10))
        assert len(canvas.shapes) == 2
        new_second_shape = canvas.shapes[1]

        # The discarded redo entry (the original second rectangle) must be
        # gone: redo() has nothing ahead to step to, so it's a no-op, not a
        # jump back to the shape that was undone.
        canvas.redo()

        assert len(canvas.shapes) == 2
        assert canvas.shapes[1] is new_second_shape

    def test_undo_after_crop_restores_pre_crop_image_and_annotations(self):
        frame = make_frame(image_size=(100, 60))
        canvas = Canvas(frame)
        canvas.resize(100, 60)

        _drag_rectangle(canvas, QPoint(5, 5), QPoint(15, 15))
        pre_crop_frame = canvas._frame
        pre_crop_shapes = canvas.shapes
        assert len(pre_crop_shapes) == 1

        canvas.set_tool(Tool.CROP)
        QTest.mousePress(canvas, Qt.MouseButton.LeftButton, pos=QPoint(10, 10))
        QTest.mouseMove(canvas, QPoint(60, 40))
        QTest.mouseRelease(canvas, Qt.MouseButton.LeftButton, pos=QPoint(60, 40))
        assert canvas.image.width() == 50
        assert len(canvas.shapes) == 0  # flattened into the cropped image

        canvas.undo()

        assert canvas._frame is pre_crop_frame
        assert canvas.image.width() == 100
        assert canvas.image.height() == 60
        assert canvas.shapes == pre_crop_shapes

    def test_undo_redo_step_exactly_one_action_at_a_time(self):
        frame = make_frame(image_size=(100, 60))
        canvas = Canvas(frame)
        canvas.resize(100, 60)

        _drag_rectangle(canvas, QPoint(5, 5), QPoint(15, 15))
        _drag_rectangle(canvas, QPoint(20, 20), QPoint(30, 30))
        _drag_rectangle(canvas, QPoint(35, 35), QPoint(45, 45))

        canvas.undo()
        assert len(canvas.shapes) == 2
        canvas.undo()
        assert len(canvas.shapes) == 1
        canvas.undo()
        assert len(canvas.shapes) == 0
        canvas.undo()  # already at the start: stays a no-op, doesn't raise
        assert len(canvas.shapes) == 0

        canvas.redo()
        assert len(canvas.shapes) == 1
        canvas.redo()
        assert len(canvas.shapes) == 2
        canvas.redo()
        assert len(canvas.shapes) == 3
        canvas.redo()  # already caught up: a no-op, doesn't raise
        assert len(canvas.shapes) == 3

    def test_editor_undo_redo_delegate_to_canvas(self):
        frame = make_frame(image_size=(100, 60))
        editor = Editor(frame)
        editor.canvas.resize(100, 60)

        _drag_rectangle(editor.canvas, QPoint(5, 5), QPoint(15, 15))
        assert len(editor.canvas.shapes) == 1

        editor.undo()
        assert len(editor.canvas.shapes) == 0

        editor.redo()
        assert len(editor.canvas.shapes) == 1

    def test_ctrl_z_triggers_undo_and_ctrl_shift_z_triggers_redo(self):
        frame = make_frame(image_size=(100, 60))
        editor = Editor(frame)
        editor.canvas.resize(100, 60)

        _drag_rectangle(editor.canvas, QPoint(5, 5), QPoint(15, 15))
        assert len(editor.canvas.shapes) == 1

        QTest.keyClick(editor, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
        assert len(editor.canvas.shapes) == 0

        QTest.keyClick(
            editor,
            Qt.Key.Key_Z,
            Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier,
        )
        assert len(editor.canvas.shapes) == 1

    def test_ctrl_z_undoes_a_shape_while_the_stroke_width_control_has_focus(self):
        # Regression test for REVIEW.md: the stroke-width QSpinBox keeps
        # Qt's default StrongFocus, and its internal QLineEdit claims
        # Ctrl+Z/Ctrl+Shift+Z for its own text-undo before the key event
        # would ever reach Editor.keyPressEvent to bubble up from it. Unlike
        # the test above, this sends the key event straight to the spinbox
        # (not to editor) so it actually exercises that dispatch path.
        frame = make_frame(image_size=(100, 60))
        editor = Editor(frame)
        editor.canvas.resize(100, 60)

        _drag_rectangle(editor.canvas, QPoint(5, 5), QPoint(15, 15))
        assert len(editor.canvas.shapes) == 1

        editor.stroke_width_spinbox.setFocus()
        QTest.keyClick(
            editor.stroke_width_spinbox, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier
        )
        assert len(editor.canvas.shapes) == 0

        QTest.keyClick(
            editor.stroke_width_spinbox,
            Qt.Key.Key_Z,
            Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier,
        )
        assert len(editor.canvas.shapes) == 1


class TestCopyOnOpen:
    def test_opening_editor_copies_the_capture_before_any_annotation(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            editor_module, "copy_image_to_clipboard", lambda image: calls.append(image)
        )
        frame = make_frame(image_size=(100, 60))

        editor = Editor(frame)

        assert len(calls) == 1
        assert calls[0] is frame.image
        # Proves "before any annotation," not just "at some point": nothing
        # above the copy call in __init__ could have added a shape, but this
        # confirms it rather than trusting call order by inspection.
        assert editor.canvas.shapes == ()


class TestCtrlSSavesRenderedImage:
    def test_ctrl_s_saves_the_rendered_annotated_image_not_the_raw_capture(
        self, monkeypatch
    ):
        frame = make_frame(image_size=(100, 60), fill_color=FILL_COLOR)
        editor = Editor(frame)
        editor.canvas.resize(100, 60)  # scale == 1.0: widget-local == image-pixel

        start = QPoint(10, 10)
        end = QPoint(60, 40)
        editor.canvas.set_colour(QColor(255, 0, 0))
        editor.canvas.set_stroke_width(4)
        _drag_rectangle(editor.canvas, start, end)
        assert len(editor.canvas.shapes) == 1

        calls = []
        monkeypatch.setattr(
            editor_module, "save_image", lambda image: calls.append(image)
        )

        QTest.keyClick(editor, Qt.Key.Key_S, Qt.KeyboardModifier.ControlModifier)

        assert len(calls) == 1
        saved = calls[0]
        assert isinstance(saved, QImage)

        # Sample the rectangle's own left border in image-pixel space (mid-
        # height, at the drag's own start.x(), mirroring the border point
        # TestEditorGrabRectangleBorder samples for the same rectangle
        # shape) — not a widget-local point reused from that grab-based
        # test, since `saved` is render()'s image-space output, not a
        # widget grab. Sampling the wrong space here would silently land in
        # the letterboxed background and pass for the wrong reason.
        sample_point = (start.x(), (start.y() + end.y()) // 2)
        assert saved.pixelColor(*sample_point) != editor.canvas.image.pixelColor(*sample_point)

    def test_ctrl_shift_s_does_not_trigger_save(self, monkeypatch):
        # Reserved for a possible future "save as": must not half-match the
        # plain Ctrl+S save, same reasoning _undo_redo_action uses to keep
        # Ctrl+Shift+Z from being swallowed as a plain Ctrl+Z.
        frame = make_frame(image_size=(100, 60))
        editor = Editor(frame)
        calls = []
        monkeypatch.setattr(
            editor_module, "save_image", lambda image: calls.append(image)
        )

        QTest.keyClick(
            editor,
            Qt.Key.Key_S,
            Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier,
        )

        assert calls == []
