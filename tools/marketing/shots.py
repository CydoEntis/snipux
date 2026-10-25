"""The still shots: each function stages one real snipux surface over the
made-up desktop and returns what it painted.

Every surface is the real widget, driven the way a person would leave it --
a selection committed, marks placed, a menu opened -- and read back with
`QWidget.grab()`. Only the desktop under it, the windows the platform
reports on it, and the OCR result are fake.
"""

from __future__ import annotations

import datetime
import os
from pathlib import Path

from PyQt6.QtCore import QPoint, QPointF, QRect, QRectF, Qt
from PyQt6.QtGui import QColor, QFont, QImage, QLinearGradient, QPainter
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QWidget

from snipux.sensitive import RecognizedWord
from snipux.shapes import Arrow, Highlighter, Rectangle, StepMarker, Text

from . import scene as desk
from .stage import SCALE, Stage, fake_text_recognition

RED = QColor("#ef4444")
ACID = QColor("#e3ff4f")
YELLOW = QColor("#facc15")

# Around the revenue chart and the team chat, with a margin.
HERO_SELECTION = QRect(888, 40, 612, 806)
# The code editor, for the recording shots.
RECORD_SELECTION = QRect(32, 40, 832, 672)

# A fixed moment for everything that shows a time; only the clock face is
# ever visible, never the date.
DEMO_TIME = datetime.datetime.combine(datetime.date.today(), datetime.time(9, 41))


def settle(ms: int = 150) -> None:
    QTest.qWait(ms)


def close_all() -> None:
    for widget in QApplication.topLevelWidgets():
        if widget.isVisible():
            widget.close()
    settle(50)


def compose(window: QWidget) -> QImage:
    """`window` as it would appear on screen: its own grab with every other
    visible top-level (popup menus, HUDs) painted over it where it sits.
    """
    image = window.grab().toImage().convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
    origin = window.geometry().topLeft()
    painter = QPainter(image)
    try:
        for other in QApplication.topLevelWidgets():
            if other is window or not other.isVisible():
                continue
            painter.drawPixmap(other.geometry().topLeft() - origin, other.grab())
    finally:
        painter.end()
    return image


def ocr_from_scene(scene: desk.Scene, selection) -> None:
    """Fake OCR reading the chat's words where the scene drew them.

    The recogniser is handed the selection's crop at capture resolution, so
    each word's logical desktop rect is moved into that crop's pixels.
    """
    def recognise(image: QImage):
        area = QRectF(selection())
        scale = image.width() / area.width()
        return [
            [RecognizedWord(text, QRectF(
                (rect.x() - area.x()) * scale, (rect.y() - area.y()) * scale,
                rect.width() * scale, rect.height() * scale,
            )) for text, rect in line]
            for line in scene.chat_lines
        ]

    fake_text_recognition(recognise)


def desktop_windows(scene: desk.Scene):
    """The scene's windows as the platform would report them on a desktop
    with a browser open: Window, Active window and Browser all have
    something to take, the way they do on a real Windows session."""
    from snipux.overlay import GeometryProvider

    windows = [
        ("Acme Analytics — Revenue", desk.REVENUE),
        ("Team chat — #launch", desk.CHAT),
        ("dashboard.py — acme-analytics", desk.EDITOR),
    ]

    class DesktopWindows(GeometryProvider):
        def is_available(self) -> bool:
            return True

        def window_at(self, point: QPointF) -> QRectF | None:
            named = self.window_named_at(point)
            return None if named is None else named[1]

        def window_named_at(self, point: QPointF):
            return next(((title, QRectF(rect)) for title, rect in windows if rect.contains(point)), None)

        def browser_viewport(self):
            # The revenue dashboard is the browser tab; its page is below
            # the title bar the scene paints.
            return windows[0][0], QRectF(desk.REVENUE).adjusted(0, 40, 0, 0)

        def active_window(self):
            return windows[2][0], QRectF(desk.EDITOR)

    return DesktopWindows()


def open_overlay(scene: desk.Scene):
    from snipux.overlay import OverlayWindow

    overlay = OverlayWindow(
        scene.frame(),
        geometry_provider=desktop_windows(scene),
        monitor_geometries=[QRectF(QPointF(0, 0), scene.logical_size)],
    )
    overlay.show_on_screen(QApplication.primaryScreen())
    # The shoot's screen is exactly the scene; a test's is whatever the
    # offscreen plugin defaults to, so the overlay is sized to the scene.
    overlay.setGeometry(QRect(QPoint(0, 0), scene.logical_size.toSize()))
    settle()
    return overlay


def full_frame(image: QImage) -> QImage:
    """`image` at the scene's physical size. It already is in a shoot; only
    a run at another scale (the tests) is resampled."""
    size = (desk.LOGICAL_SIZE * SCALE).toSize()
    if image.size() == size:
        return image
    return image.scaled(size, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)


def hero_marks(scene: desk.Scene) -> list:
    """The annotation every hero-style shot carries, in window coordinates."""
    places = scene.places
    headline = places["headline"].adjusted(-6, -6, 6, 6)
    bar = places["best_bar"]
    message = places["message0"]
    tip = QPointF(bar.center().x() - 4, bar.top() - 14)
    return [
        Rectangle(colour=RED, stroke_width=4, start=headline.topLeft(), end=headline.bottomRight()),
        StepMarker(colour=RED, stroke_width=4, point=QPointF(headline.right() + 4, headline.top() + 2), number=1),
        Arrow(colour=ACID, stroke_width=5, start=QPointF(tip.x() - 250, tip.y() + 150), end=tip),
        StepMarker(colour=RED, stroke_width=4, point=QPointF(tip.x() + 2, tip.y() - 30), number=2),
        Text(colour=ACID, stroke_width=6, text="Best week yet", point=QPointF(tip.x() - 420, tip.y() + 150)),
        Highlighter(colour=YELLOW, stroke_width=5,
                    points=[message.topLeft(), message.topRight()],
                    bands=[message.adjusted(-4, -2, 4, 2)]),
    ]


def annotated_overlay(scene: desk.Scene):
    """The hero state: a committed selection, Hide sensitive on, marks."""
    overlay = open_overlay_with_ocr(scene)
    overlay._commit_selection(HERO_SELECTION)
    for mark in hero_marks(scene):
        overlay.add_mark(mark)
    settle(1500)
    return overlay


def open_overlay_with_ocr(scene: desk.Scene):
    # Installed before the overlay exists: the chooser decides as it is
    # built whether Hide sensitive is offered at all.
    built = []
    ocr_from_scene(scene, lambda: built[0]._selection)
    overlay = open_overlay(scene)
    built.append(overlay)
    overlay._chooser.set_hide_sensitive(True)
    return overlay


# -- shots --------------------------------------------------------------


def hero(stage: Stage) -> QImage:
    scene = desk.build_scene()
    overlay = annotated_overlay(scene)
    image = full_frame(compose(overlay))
    close_all()
    return image


def chooser(stage: Stage) -> QImage:
    scene = desk.build_scene()
    overlay = open_overlay_with_ocr(scene)
    overlay._chooser._toggle_menu()
    settle(400)
    image = full_frame(compose(overlay))
    close_all()
    return image


def recordings_open_in_the_player() -> None:
    """Make Open in the player the recording destination, as Settings would,
    so the chooser and the bar say where the clip's recording goes."""
    from snipux import setup_desktop

    setup_desktop.save_recording_after("open")


def record(stage: Stage) -> QImage:
    scene = desk.build_scene()
    recordings_open_in_the_player()
    overlay = open_overlay(scene)
    overlay._chooser.set_kind("record")
    overlay._commit_selection(RECORD_SELECTION)
    place_recording_bar(overlay, QRectF(RECORD_SELECTION))
    settle(400)
    image = full_frame(compose(overlay))
    close_all()
    return image


def place_recording_bar(overlay, region: QRectF):
    """The ready bar the controller would show, embedded in the overlay the
    way app.py does where it cannot place windows of its own."""
    from snipux.app import _place_recording_hud
    from snipux.flowbars import RecordingBar

    bar = RecordingBar(overlay)
    bar.set_ready()
    bar.set_destination(overlay.outcome)
    bar.set_backdrop_host(overlay)
    bar.adjustSize()
    spot = _place_recording_hud(region, [QRectF(overlay.rect())], bar.sizeHint())
    if spot is None:
        # No room by the app's rules, which happens with Linux CI's wider
        # fonts. The published shots are rendered on Windows (README), so
        # here it only has to land somewhere for the test to render it.
        spot = QRectF(region.center().x() - bar.width() / 2, region.bottom(),
                      bar.width(), bar.height())
    bar.move(QRectF(spot).center().toPoint() - bar.rect().center())
    bar.show()
    bar.raise_()
    return bar


def review(stage: Stage) -> QImage:
    from snipux.review import ReviewWindow

    scene = desk.build_scene()
    overlay = annotated_overlay(scene)
    snip = overlay.rendered_image()
    close_all()
    path = save_demo_png(stage, snip, "Launch numbers.png")
    window = ReviewWindow(snip, saved_path=path)
    window.show()
    window._set_annotating(True)
    settle(400)
    image = on_desktop(compose(window), scene)
    close_all()
    return image


def settings(stage: Stage) -> QImage:
    from snipux.settings import SettingsWindow

    window = SettingsWindow()
    window.show()
    settle(400)
    image = on_desktop(compose(window), desk.build_scene())
    close_all()
    return image


def pin(stage: Stage) -> QImage:
    from snipux.pin import PinWindow

    scene = desk.build_scene()
    overlay = annotated_overlay(scene)
    overlay.set_selection(desk.REVENUE.toRect())
    snip = overlay.rendered_image()
    close_all()
    # Dragged off the spot it was cut from, over the editor, the way a pin
    # is used: kept in view beside the work it is for.
    rect = QRect(250, 300, 580, 412)
    window = PinWindow(snip, rect)
    window.show()
    settle(300)
    canvas = desktop_canvas(scene, dim=0.0)
    paint_floating(canvas, compose(window), QPointF(rect.topLeft()), radius=8)
    close_all()
    return canvas


def history(stage: Stage) -> QImage:
    from snipux import output
    from snipux.history import PANEL_W, HistoryPanel

    scene = desk.build_scene()
    overlay = annotated_overlay(scene)
    snips = [overlay.rendered_image()]
    for rect in (RECORD_SELECTION, desk.CHAT.toRect()):
        overlay.set_selection(rect)
        snips.append(overlay.rendered_image())
    close_all()
    copied = [
        output.CopiedItem("image", snips[0], DEMO_TIME),
        output.CopiedItem("text", "load_orders(week)", DEMO_TIME - datetime.timedelta(minutes=3)),
        output.CopiedItem("image", snips[2], DEMO_TIME - datetime.timedelta(minutes=9)),
    ]
    saved = [save_demo_png(stage, snips[1], "Weekly summary.png")]
    panel = HistoryPanel(copied, saved)
    gap = 10
    geometry = QRect(1600 - PANEL_W - gap, gap, PANEL_W, int(1000 - desk.TASKBAR_H) - 2 * gap)
    panel.setGeometry(geometry)
    panel.show()
    settle(300)
    canvas = desktop_canvas(scene, dim=0.0)
    paint_floating(canvas, compose(panel), QPointF(geometry.topLeft()), radius=0)
    close_all()
    return canvas


def og_image(hero_image: QImage) -> QImage:
    """1200x630 for link previews: the name and one line on the left, the
    hero's annotated selection, bar and all, on the right."""
    from snipux.design import PACKAGE_DIR, font_families

    card = QImage(1200, 630, QImage.Format.Format_ARGB32_Premultiplied)
    painter = QPainter(card)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    try:
        backdrop = QLinearGradient(0, 0, 1200, 630)
        backdrop.setColorAt(0.0, QColor("#141a33"))
        backdrop.setColorAt(1.0, QColor("#4a1f48"))
        painter.fillRect(card.rect(), backdrop)

        source = hero_image.copy()
        source.setDevicePixelRatio(1.0)
        area = HERO_SELECTION.adjusted(-20, -36, 20, 110)
        crop = source.copy(QRect(area.x() * SCALE, area.y() * SCALE,
                                 area.width() * SCALE, area.height() * SCALE))
        shot = crop.scaledToHeight(590, Qt.TransformationMode.SmoothTransformation)
        painter.drawImage(QPointF(1200 - shot.width() - 24, 20), shot)

        logo = QImage(str(PACKAGE_DIR / "design" / "logo" / "snipux-128.png"))
        painter.drawImage(QRectF(64, 178, 72, 72), logo)
        families = font_families()
        title = QFont(families.ui)
        title.setPixelSize(64)
        title.setWeight(QFont.Weight.Bold)
        painter.setFont(title)
        painter.setPen(QColor("#f4f6fb"))
        painter.drawText(QPointF(154, 238), "Snipux")
        body = QFont(families.ui)
        body.setPixelSize(28)
        painter.setFont(body)
        painter.setPen(QColor("#c3c8d6"))
        painter.drawText(QRectF(64, 290, 520, 140), Qt.TextFlag.TextWordWrap,
                         "Snip, annotate and record your screen. On Linux and Windows.")
    finally:
        painter.end()
    return card


# -- helpers --------------------------------------------------------------


def save_demo_png(stage: Stage, image: QImage, name: str) -> Path:
    path = stage.pictures() / name
    if not image.save(str(path), "PNG"):
        raise OSError(f"could not write {path}")
    stamp = DEMO_TIME.timestamp()
    os.utime(path, (stamp, stamp))
    return path


def desktop_canvas(scene: desk.Scene, *, dim: float) -> QImage:
    canvas = scene.image.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
    canvas.setDevicePixelRatio(SCALE)
    if dim:
        painter = QPainter(canvas)
        painter.fillRect(QRectF(QPointF(0, 0), scene.logical_size), QColor(0, 0, 0, int(255 * dim)))
        painter.end()
    return canvas


def paint_floating(canvas: QImage, window: QImage, top_left: QPointF, *, radius: float) -> None:
    """Paint `window` onto `canvas` at `top_left` (logical) with a soft shadow."""
    size = QRectF(top_left, QPointF(top_left.x() + window.width() / window.devicePixelRatio(),
                                    top_left.y() + window.height() / window.devicePixelRatio()))
    painter = QPainter(canvas)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    try:
        painter.setPen(Qt.PenStyle.NoPen)
        for i in range(24, 0, -2):
            painter.setBrush(QColor(0, 0, 0, 7))
            painter.drawRoundedRect(size.adjusted(-i, -i + 14, i, i + 14), radius + i, radius + i)
        painter.drawImage(top_left, window)
    finally:
        painter.end()


def on_desktop(window: QImage, scene: desk.Scene) -> QImage:
    """A window centred over the dimmed desktop, above the taskbar."""
    canvas = desktop_canvas(scene, dim=0.45)
    w = window.width() / window.devicePixelRatio()
    h = window.height() / window.devicePixelRatio()
    area_h = scene.logical_size.height() - desk.TASKBAR_H
    top_left = QPointF((scene.logical_size.width() - w) / 2, max(16.0, (area_h - h) / 2))
    paint_floating(canvas, window, top_left, radius=10)
    return canvas


STILLS = {
    "hero": hero,
    "chooser": chooser,
    "record": record,
    "review": review,
    "settings": settings,
    "pin": pin,
    "history": history,
}
