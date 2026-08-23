import pytest
from PyQt6.QtCore import QPoint, QPointF, QSizeF, Qt
from PyQt6.QtGui import QColor, QImage, qRgb
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication

from snipux.capture import Frame
from snipux.editor import Canvas, Editor, Tool
from snipux.shapes import Rectangle, StepMarker, Text

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
    # Canvas only ever reads Frame.image, never Frame.logical_*, so
    # logical_origin/logical_size are given arbitrary placeholder values
    # rather than anything meaningful to this test.
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
