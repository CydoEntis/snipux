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


def make_frame(image_size=(100, 60), fill_color=FILL_COLOR, logical_origin=None) -> Frame:
    # scale 1.0 (logical_size == image_size in pixels) so widget-local
    # coordinates line up with image pixels 1:1, including for the crop
    # tests, which read logical_origin/logical_size through apply_crop().
    image = QImage(*image_size, QImage.Format.Format_RGB32)
    image.fill(fill_color)
    if logical_origin is None:
        logical_origin = QPointF(0, 0)
    return Frame(
        image=image,
        logical_origin=logical_origin,
        logical_size=QSizeF(*image_size),
    )


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


class TestOutsideWidget:
    def test_widget_to_image_returns_none_outside_widget_bounds(self):
        # No letterbox margin exists any more (SNX-21): _target_rect fills
        # the widget exactly, so the only "outside the image" a point can be
        # is genuinely outside the widget's own rect.
        frame = make_frame(image_size=(100, 60))
        canvas = Canvas(frame)
        canvas.resize(100, 60)

        assert canvas.widget_to_image(QPointF(-1, -1)) is None
        assert canvas.widget_to_image(QPointF(150, 30)) is None
        assert canvas.widget_to_image(QPointF(50, 30)) is not None


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


class TestToolbarControls:
    def test_tool_actions_cover_every_tool_and_switching_updates_canvas(self):
        frame = make_frame()
        editor = Editor(frame)

        assert set(editor.tool_actions) == set(Tool)
        assert editor.canvas._tool is not None  # a usable default is armed

        editor.tool_actions[Tool.RECTANGLE].trigger()

        assert editor.canvas._tool is Tool.RECTANGLE
        assert editor.tool_actions[Tool.RECTANGLE].isChecked()

    def test_common_tools_lead_the_main_row(self):
        # SNX-26: pen, highlighter, eraser and crop are the tools the user
        # actually reached for, so they -- and only they -- sit directly on
        # the toolbar, in that order.
        frame = make_frame()
        editor = Editor(frame)
        action_to_tool = {action: tool for tool, action in editor.tool_actions.items()}

        main_row_tools = [
            action_to_tool[action]
            for action in editor.toolbar.actions()
            if action in action_to_tool
        ]

        assert main_row_tools == [Tool.PEN, Tool.HIGHLIGHTER, Tool.ERASER, Tool.CROP]

    def test_remaining_tools_are_reachable_through_the_more_tools_menu(self):
        # Not removed, not hidden -- just one click away instead of
        # crowding the main row (per SNX-26's "no tool is to be removed").
        frame = make_frame()
        editor = Editor(frame)
        action_to_tool = {action: tool for tool, action in editor.tool_actions.items()}
        expected_secondary_tools = set(Tool) - set(Editor.PRIMARY_TOOLS)

        menu_tools = {
            action_to_tool[action]
            for action in editor.more_tools_menu.actions()
            if action in action_to_tool
        }

        assert menu_tools == expected_secondary_tools
        # And none of them leaked into the main row alongside the primary four.
        main_row_tools = {
            action_to_tool[action]
            for action in editor.toolbar.actions()
            if action in action_to_tool
        }
        assert main_row_tools.isdisjoint(expected_secondary_tools)

    def test_no_tool_label_displays_an_underscore(self):
        # Tool.STEP_MARKER's raw enum value ("step_marker") must not leak
        # into the UI verbatim.
        frame = make_frame()
        editor = Editor(frame)

        for action in editor.tool_actions.values():
            assert "_" not in action.text()
        assert editor.tool_actions[Tool.STEP_MARKER].text() == "Step Marker"

    def test_main_row_fits_without_overflow_at_the_editors_normal_width(self):
        # 1920: a typical full-HD monitor width, same "normal" screen size
        # test_capture.py's multi-monitor fixtures use elsewhere -- a stand-in
        # for "a real capture," not a tiny fixture like the 100x60 default
        # `make_frame()` most of this file uses. The eleven-action row this
        # ticket replaced overflowed into QToolBar's own chevron on a real
        # session; the trimmed-down main row (four tools, swatches, custom
        # colour, stroke width, undo/redo/clear, copy/save/done) must not.
        frame = make_frame(image_size=(1920, 1080))
        editor = Editor(frame)

        assert editor.toolbar.sizeHint().width() <= frame.image.width()

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


class TestDefaultColour:
    # SNX-25: black-by-default made the first strokes on a dark capture
    # invisible, reading as "drawing is broken" rather than "pick a colour."

    def test_default_annotation_colour_is_red_not_black(self):
        frame = make_frame()
        editor = Editor(frame)

        assert editor.canvas._colour == QColor(Qt.GlobalColor.red)

    def test_bare_canvas_also_defaults_to_red(self):
        # Editor.__init__ sets the colour explicitly, but Canvas's own
        # default (used whenever a caller builds one directly, as most of
        # this file's tests do) must not silently regress back to black.
        frame = make_frame()
        canvas = Canvas(frame)

        assert canvas._colour == QColor(Qt.GlobalColor.red)


class TestColourPicker:
    # SNX-25: swatches alone couldn't reach an arbitrary colour, and a
    # QColorDialog was explicitly out of scope for the ticket that added
    # them. This adds one, gated so it never opens on its own.

    def test_toolbar_offers_a_colour_picker_action(self):
        frame = make_frame()
        editor = Editor(frame)

        assert editor.colour_picker_action in editor.toolbar.actions()

    def test_no_dialog_is_opened_while_constructing_the_editor(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            editor_module.QColorDialog,
            "getColor",
            staticmethod(lambda *args, **kwargs: calls.append((args, kwargs)) or QColor()),
        )

        Editor(make_frame())

        assert calls == []

    def test_picking_a_colour_becomes_the_colour_used_by_later_annotations(self, monkeypatch):
        frame = make_frame(image_size=(100, 60))
        editor = Editor(frame)
        editor.canvas.resize(100, 60)
        chosen = QColor(12, 34, 56)
        monkeypatch.setattr(
            editor_module.QColorDialog,
            "getColor",
            staticmethod(lambda *args, **kwargs: chosen),
        )

        editor.colour_picker_action.trigger()
        assert editor.canvas._colour == chosen

        _drag_rectangle(editor.canvas, QPoint(5, 5), QPoint(15, 15))
        assert editor.canvas.shapes[0].colour == chosen

    def test_cancelling_the_dialog_leaves_the_colour_unchanged(self, monkeypatch):
        frame = make_frame()
        editor = Editor(frame)
        red = QColor(editor.canvas._colour)
        # QColorDialog.getColor() returns an invalid QColor on Cancel,
        # rather than raising or returning None.
        monkeypatch.setattr(
            editor_module.QColorDialog,
            "getColor",
            staticmethod(lambda *args, **kwargs: QColor()),
        )

        editor.colour_picker_action.trigger()

        assert editor.canvas._colour == red

    def test_colour_swatches_are_still_offered_alongside_the_picker(self):
        frame = make_frame()
        editor = Editor(frame)

        assert len(editor.colour_buttons) > 0
        assert len(editor.colour_buttons) <= 5


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


def _erase_at(canvas: Canvas, pos: QPoint) -> None:
    canvas.set_tool(Tool.ERASER)
    QTest.mousePress(canvas, Qt.MouseButton.LeftButton, pos=pos)
    QTest.mouseRelease(canvas, Qt.MouseButton.LeftButton, pos=pos)


class TestEraserTool:
    def test_toolbar_offers_an_eraser_tool(self):
        # The generic "every Tool member gets an action" coverage lives in
        # TestToolbarControls; this pins the specific tool this ticket adds,
        # including that armed it actually reaches Canvas like any other.
        frame = make_frame(image_size=(100, 60))
        editor = Editor(frame)

        assert Tool.ERASER in editor.tool_actions

        editor.tool_actions[Tool.ERASER].trigger()

        assert editor.canvas._tool is Tool.ERASER

    def test_click_on_annotation_removes_only_that_shape(self):
        frame = make_frame(image_size=(100, 60))
        canvas = Canvas(frame)
        canvas.resize(100, 60)  # scale == 1.0: widget-local == image-pixel

        _drag_rectangle(canvas, QPoint(5, 5), QPoint(15, 15))
        _drag_rectangle(canvas, QPoint(40, 40), QPoint(50, 50))
        assert len(canvas.shapes) == 2
        kept = canvas.shapes[1]

        # (5, 10) sits on the first rectangle's left border, well clear of
        # the second rectangle's hit region.
        _erase_at(canvas, QPoint(5, 10))

        assert canvas.shapes == (kept,)

    def test_click_on_empty_space_leaves_every_shape_in_place(self):
        frame = make_frame(image_size=(100, 60))
        canvas = Canvas(frame)
        canvas.resize(100, 60)

        _drag_rectangle(canvas, QPoint(5, 5), QPoint(15, 15))
        all_shapes = canvas.shapes

        _erase_at(canvas, QPoint(80, 50))  # nowhere near the rectangle's border

        assert canvas.shapes == all_shapes

    def test_overlapping_shapes_erase_removes_the_most_recently_drawn(self):
        frame = make_frame(image_size=(100, 60))
        canvas = Canvas(frame)
        canvas.resize(100, 60)

        # Two identically-placed rectangles: a click anywhere on their
        # shared border hits both, so this is what actually exercises
        # "most recently drawn wins" rather than distinguishing them by
        # position.
        _drag_rectangle(canvas, QPoint(5, 5), QPoint(25, 25))
        _drag_rectangle(canvas, QPoint(5, 5), QPoint(25, 25))
        first, second = canvas.shapes
        assert first is not second

        _erase_at(canvas, QPoint(5, 15))  # on the shared left border

        assert canvas.shapes == (first,)

    def test_erase_is_undoable_as_a_single_step(self):
        frame = make_frame(image_size=(100, 60))
        canvas = Canvas(frame)
        canvas.resize(100, 60)

        _drag_rectangle(canvas, QPoint(5, 5), QPoint(15, 15))
        _drag_rectangle(canvas, QPoint(40, 40), QPoint(50, 50))
        all_shapes = canvas.shapes

        _erase_at(canvas, QPoint(5, 10))
        assert len(canvas.shapes) == 1

        canvas.undo()

        assert canvas.shapes == all_shapes


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


class TestUndoRedoClearToolbarControls:
    def test_undo_and_redo_actions_start_disabled(self):
        frame = make_frame(image_size=(100, 60))
        editor = Editor(frame)

        assert not editor.undo_action.isEnabled()
        assert not editor.redo_action.isEnabled()

    def test_actions_enable_and_disable_as_shapes_are_added_undone_and_redone(self):
        frame = make_frame(image_size=(100, 60))
        editor = Editor(frame)
        editor.canvas.resize(100, 60)

        _drag_rectangle(editor.canvas, QPoint(5, 5), QPoint(15, 15))
        assert editor.undo_action.isEnabled()
        assert not editor.redo_action.isEnabled()  # nothing ahead yet

        editor.undo_action.trigger()
        assert len(editor.canvas.shapes) == 0
        assert not editor.undo_action.isEnabled()  # back at the starting state
        assert editor.redo_action.isEnabled()

        editor.redo_action.trigger()
        assert len(editor.canvas.shapes) == 1
        assert editor.undo_action.isEnabled()
        assert not editor.redo_action.isEnabled()  # caught back up

    def test_new_shape_after_undo_disables_redo_again(self):
        # Mirrors TestUndoRedo.test_new_shape_after_undo_discards_the_stale_
        # redo_entry: the stale redo entry is gone, so the control reflects
        # that rather than staying enabled from before the new shape.
        frame = make_frame(image_size=(100, 60))
        editor = Editor(frame)
        editor.canvas.resize(100, 60)

        _drag_rectangle(editor.canvas, QPoint(5, 5), QPoint(15, 15))
        _drag_rectangle(editor.canvas, QPoint(20, 20), QPoint(30, 30))
        editor.undo_action.trigger()
        assert editor.redo_action.isEnabled()

        _drag_rectangle(editor.canvas, QPoint(50, 5), QPoint(55, 10))

        assert not editor.redo_action.isEnabled()

    def test_clear_action_removes_every_shape_in_one_step(self):
        frame = make_frame(image_size=(100, 60))
        editor = Editor(frame)
        editor.canvas.resize(100, 60)

        _drag_rectangle(editor.canvas, QPoint(5, 5), QPoint(15, 15))
        _drag_rectangle(editor.canvas, QPoint(20, 20), QPoint(30, 30))
        _drag_rectangle(editor.canvas, QPoint(35, 35), QPoint(45, 45))
        all_three = editor.canvas.shapes
        assert len(all_three) == 3

        editor.clear_action.trigger()

        assert editor.canvas.shapes == ()

    def test_undo_after_clear_restores_every_shape_that_was_cleared(self):
        frame = make_frame(image_size=(100, 60))
        editor = Editor(frame)
        editor.canvas.resize(100, 60)

        _drag_rectangle(editor.canvas, QPoint(5, 5), QPoint(15, 15))
        _drag_rectangle(editor.canvas, QPoint(20, 20), QPoint(30, 30))
        all_two = editor.canvas.shapes

        editor.clear_action.trigger()
        assert editor.canvas.shapes == ()

        editor.undo_action.trigger()

        assert editor.canvas.shapes == all_two

    def test_clear_on_an_empty_canvas_is_a_no_op(self):
        # No shapes to clear: must not push a history entry an undo could
        # then "restore" nothing meaningful from.
        frame = make_frame(image_size=(100, 60))
        editor = Editor(frame)

        editor.canvas.clear()

        assert editor.canvas.shapes == ()
        assert not editor.undo_action.isEnabled()


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


class TestCopySaveDoneToolbarControls:
    def test_copy_action_copies_the_rendered_annotated_image_not_the_raw_capture(
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
            editor_module, "copy_image_to_clipboard", lambda image: calls.append(image)
        )

        editor.copy_action.trigger()

        assert len(calls) == 1
        copied = calls[0]
        assert isinstance(copied, QImage)
        # Same sampling point TestCtrlSSavesRenderedImage uses for the same
        # rectangle shape, in the render() image-pixel space _copy() builds.
        sample_point = (start.x(), (start.y() + end.y()) // 2)
        assert copied.pixelColor(*sample_point) != editor.canvas.image.pixelColor(*sample_point)

    def test_save_action_saves_the_rendered_annotated_image(self, monkeypatch):
        frame = make_frame(image_size=(100, 60), fill_color=FILL_COLOR)
        editor = Editor(frame)
        editor.canvas.resize(100, 60)

        _drag_rectangle(editor.canvas, QPoint(10, 10), QPoint(60, 40))
        assert len(editor.canvas.shapes) == 1

        calls = []
        monkeypatch.setattr(
            editor_module, "save_image", lambda image: calls.append(image)
        )

        editor.save_action.trigger()

        assert len(calls) == 1
        assert isinstance(calls[0], QImage)

    def test_done_action_closes_the_editor(self):
        frame = make_frame(image_size=(100, 60))
        editor = Editor(frame)
        editor.show()
        assert editor.isVisible()

        editor.done_action.trigger()

        assert not editor.isVisible()

    def test_ctrl_s_still_saves_after_adding_the_toolbar_controls(self, monkeypatch):
        # Pins the ticket's "existing Ctrl+S binding still saves" criterion
        # against regressing once Save became reachable from the toolbar too.
        frame = make_frame(image_size=(100, 60))
        editor = Editor(frame)
        calls = []
        monkeypatch.setattr(
            editor_module, "save_image", lambda image: calls.append(image)
        )

        QTest.keyClick(editor, Qt.Key.Key_S, Qt.KeyboardModifier.ControlModifier)

        assert len(calls) == 1


class TestWindowPlacement:
    # SNX-21: the editor used to appear as an ordinary titled window wherever
    # the window manager put it, sized by layout rather than by the snip --
    # these pin the fix instead: frameless/always-on-top, and positioned so
    # the image sits exactly over the screen region it was captured from.

    def test_frameless_and_stays_on_top(self):
        frame = make_frame(image_size=(100, 60))
        editor = Editor(frame)

        flags = editor.windowFlags()
        assert flags & Qt.WindowType.FramelessWindowHint
        assert flags & Qt.WindowType.WindowStaysOnTopHint

    def test_canvas_matches_image_size_and_sits_at_the_snips_logical_origin(self):
        frame = make_frame(image_size=(100, 60), logical_origin=QPointF(300, 150))
        editor = Editor(frame)

        # 1:1, no scaling: the canvas's own size is exactly the captured
        # image's size, not fit-to-window.
        assert editor.canvas.size().width() == 100
        assert editor.canvas.size().height() == 60

        # It's the *canvas's* global position that must land on
        # logical_origin, not the window's -- the toolbar sits above it (see
        # below), so the window itself starts higher and is taller.
        canvas_global_top_left = editor.canvas.mapToGlobal(QPoint(0, 0))
        assert canvas_global_top_left == QPoint(300, 150)

    def test_toolbar_sits_above_the_canvas_without_overlapping_it(self):
        frame = make_frame(image_size=(100, 60))
        editor = Editor(frame)

        # The toolbar occupies space strictly above the canvas within the
        # window -- it must never dip into the rect the snipped image is
        # drawn into, per the ticket's "does not cover the snipped image"
        # criterion.
        assert editor.toolbar.geometry().bottom() <= editor.canvas.geometry().top()


class TestDimmedVeilAndBorder:
    # SNX-28: canvas used to be pixel-identical to the live desktop beneath
    # it -- same pixels, no frame, no border, no shadow -- so a user could
    # not tell where the snip ended and the live desktop began, and drew
    # strokes believing they were off the snip when they were on it.
    # Overlay already solves the same problem during selection by dimming
    # everywhere outside the selected rect; these pin the same treatment
    # here, plus a border, without either leaking into a copy or save.

    DESKTOP_FILL = qRgb(220, 220, 220)
    SNIP_FILL = qRgb(10, 20, 30)

    def _make_editor(self) -> Editor:
        # The desktop is much bigger than the snip and offset from its
        # origin, so a sampled "outside the snip" point can't accidentally
        # land on the snip by coincidence.
        desktop_frame = make_frame(
            image_size=(400, 300), fill_color=self.DESKTOP_FILL, logical_origin=QPointF(0, 0)
        )
        snip_frame = make_frame(
            image_size=(100, 60), fill_color=self.SNIP_FILL, logical_origin=QPointF(150, 120)
        )
        return Editor(snip_frame, desktop_frame)

    def test_area_outside_the_snip_is_visibly_dimmed(self):
        editor = self._make_editor()
        rendered = editor.grab().toImage()

        point = editor._desktop_local_rect.topLeft() + QPointF(5, 5)
        assert not editor._snip_local_rect.contains(point)  # genuinely outside the snip
        sampled = rendered.pixelColor(round(point.x()), round(point.y()))

        raw = QColor(self.DESKTOP_FILL)
        assert sampled != raw  # not the raw, undimmed desktop pixel
        # A veil darkens rather than recolours: every channel is at or
        # below the raw fill's own.
        assert sampled.red() <= raw.red()
        assert sampled.green() <= raw.green()
        assert sampled.blue() <= raw.blue()

    def test_snip_pixels_are_undimmed_and_exact(self):
        editor = self._make_editor()
        rendered = editor.grab().toImage()

        image_point = QPointF(50, 30)
        widget_point = editor.canvas.image_to_widget(image_point) + QPointF(editor.canvas.pos())
        sampled = rendered.pixelColor(round(widget_point.x()), round(widget_point.y()))

        assert sampled == QColor(self.SNIP_FILL)

    def test_a_border_marks_the_snips_edge(self):
        editor = self._make_editor()
        rendered = editor.grab().toImage()

        snip_rect = editor._snip_local_rect
        # Just outside the snip's left edge -- outside canvas (so nothing
        # painted on top of it), one pixel clear of the boundary line.
        border_point = QPoint(round(snip_rect.left()) - 1, round(snip_rect.center().y()))
        sampled = rendered.pixelColor(border_point)

        assert sampled == editor.BORDER_COLOR

    def test_toolbar_remains_legible_against_the_dimmed_backdrop(self):
        editor = self._make_editor()
        rendered = editor.grab().toImage()

        dimmed_point = editor._desktop_local_rect.topLeft() + QPointF(5, 5)
        dimmed_pixel = rendered.pixelColor(round(dimmed_point.x()), round(dimmed_point.y()))

        toolbar_center = editor.toolbar.geometry().center()
        toolbar_pixel = rendered.pixelColor(toolbar_center)

        # The toolbar is an ordinary opaque child widget painted on top of
        # the veil, not a transparent one the dimmed backdrop shows through
        # -- proven here by it not sharing the dimmed area's own colour.
        assert toolbar_pixel != dimmed_pixel

    def test_dimming_and_border_are_absent_from_the_saved_image(self, monkeypatch):
        editor = self._make_editor()
        calls = []
        monkeypatch.setattr(
            editor_module, "save_image", lambda image: calls.append(image)
        )

        editor.save_action.trigger()

        assert len(calls) == 1
        saved = calls[0]
        # Exactly the snip's own size, not the (much larger) desktop this
        # window now spans -- a save/copy that picked up any dimmed margin
        # or border would be bigger than the raw capture.
        assert saved.size() == editor.canvas.image.size()
        assert saved.pixelColor(0, 0) == QColor(self.SNIP_FILL)
        assert saved.pixelColor(saved.width() - 1, saved.height() - 1) == QColor(self.SNIP_FILL)

    def test_dimming_and_border_are_absent_from_the_copied_image(self, monkeypatch):
        editor = self._make_editor()
        calls = []
        monkeypatch.setattr(
            editor_module, "copy_image_to_clipboard", lambda image: calls.append(image)
        )

        editor.copy_action.trigger()

        assert len(calls) == 1
        copied = calls[0]
        assert copied.size() == editor.canvas.image.size()
        assert copied.pixelColor(0, 0) == QColor(self.SNIP_FILL)

    def test_no_desktop_frame_given_dims_nothing(self):
        # The many existing single-argument Editor(frame) callers (this
        # file's other test classes among them) get exactly SNX-21's
        # tightly-sized window back, with no desktop area to dim -- passing
        # no `desktop_frame` must not be a behaviour change for them.
        frame = make_frame(image_size=(100, 60), fill_color=self.SNIP_FILL)
        editor = Editor(frame)

        assert editor._desktop_local_rect == editor._snip_local_rect
        assert editor.size().width() == 100
        assert editor.size().height() == 60 + editor.toolbar.sizeHint().height()


class TestEscapeClosesWithoutSaving:
    def test_escape_closes_the_editor_without_saving(self, monkeypatch):
        frame = make_frame(image_size=(100, 60))
        editor = Editor(frame)
        editor.show()
        calls = []
        monkeypatch.setattr(
            editor_module, "save_image", lambda image: calls.append(image)
        )

        assert editor.isVisible()
        QTest.keyClick(editor, Qt.Key.Key_Escape)

        assert calls == []
        assert not editor.isVisible()
