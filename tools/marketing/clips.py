"""The demo clips, and the player shot that needs a real video to open.

Each clip drives the real overlay, bars and player through the same events a
person's input would send -- mouse presses and drags, tool keys -- frame by
frame. What is simulated is only what snipux does not draw: the desktop,
the pointer, the keys on screen, and the operating system's own
notification balloon.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from PyQt6.QtCore import QEvent, QPoint, QPointF, QRect, QRectF, Qt
from PyQt6.QtGui import QColor, QFont, QImage, QKeyEvent, QMouseEvent, QPainter, QPen
from PyQt6.QtWidgets import QApplication, QWidget

from snipux import design
from snipux.design import PACKAGE_DIR, font_families

from . import scene as desk
from . import shots
from .reel import FPS, Encoder, Reel, ffmpeg_binary
from .shots import RECORD_SELECTION, close_all, compose, on_desktop, settle
from .stage import SCALE, Stage

RECORDING_SECONDS = 12
RECORDING_NAME = "Recording.mp4"


# -- input ------------------------------------------------------------------


def mouse(widget: QWidget, kind: QEvent.Type, at: QPointF) -> None:
    """Send one left-button mouse event to `widget` at `at`, in its own
    coordinates. A move is a drag: the button is held through it."""
    left = Qt.MouseButton.LeftButton
    none = Qt.MouseButton.NoButton
    button, held = {
        QEvent.Type.MouseButtonPress: (left, left),
        QEvent.Type.MouseMove: (none, left),
        QEvent.Type.MouseButtonRelease: (left, none),
    }[kind]
    event = QMouseEvent(kind, at, widget.mapToGlobal(at), button, held, Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(widget, event)


def press(widget: QWidget, at: QPointF) -> None:
    mouse(widget, QEvent.Type.MouseButtonPress, at)
    mouse(widget, QEvent.Type.MouseButtonRelease, at)


def key(widget: QWidget, code: Qt.Key, text: str = "", modifiers=Qt.KeyboardModifier.NoModifier) -> None:
    for kind in (QEvent.Type.KeyPress, QEvent.Type.KeyRelease):
        QApplication.sendEvent(widget, QKeyEvent(kind, code, modifiers, text))


def centre_in(widget: QWidget, host: QWidget) -> QPointF:
    return QPointF(widget.mapTo(host, widget.rect().center()))


def click_widget(reel: Reel, widget: QWidget, host: QWidget, seconds: float = 0.6) -> None:
    """Move the pointer onto `widget` and click it with real mouse events."""
    reel.move(centre_in(widget, host), seconds)
    press(widget, QPointF(widget.rect().center()))
    reel.click()


def drag(reel: Reel, overlay: QWidget, start: QPointF, end: QPointF, seconds: float) -> None:
    """Press at `start`, drag to `end`, release -- on the overlay itself."""
    reel.move(start, 0.5)
    mouse(overlay, QEvent.Type.MouseButtonPress, start)
    reel.click()
    reel.move(end, seconds, lambda at: mouse(overlay, QEvent.Type.MouseMove, at))
    mouse(overlay, QEvent.Type.MouseButtonRelease, end)


def tool_key(reel: Reel, overlay: QWidget, letter: str) -> None:
    key(overlay, getattr(Qt.Key, f"Key_{letter}"), letter.lower())
    reel.keys([letter], 0.9)


# -- the recorded video ------------------------------------------------------


def typed_at(t: float) -> int:
    """How much of the typed line exists `t` seconds into the recording:
    one character at a time from 2s to 8s."""
    chars = len("".join(text for text, _c in desk.TYPED_LINE))
    return round(chars * min(1.0, max(0.0, (t - 2.0) / 6.0)))


def recorded_frame(t: float) -> QImage:
    """What the recording of the editor shows `t` seconds in."""
    scene = desk.build_scene(typed=typed_at(t))
    region = RECORD_SELECTION
    return scene.image.copy(QRect(int(region.x() * SCALE), int(region.y() * SCALE),
                                  int(region.width() * SCALE), int(region.height() * SCALE)))


def make_recording(stage: Stage) -> Path | None:
    """The video the player opens, as snipux would have written it: H.264
    with a sound track (a soft synthetic tone, so the waveform has a shape)."""
    binary = ffmpeg_binary()
    if binary is None:
        print("ffmpeg not found: skipping the player shot, which needs a real video file.")
        return None
    path = stage.videos() / RECORDING_NAME
    if path.exists():
        return path
    width, height = int(RECORD_SELECTION.width() * SCALE), int(RECORD_SELECTION.height() * SCALE)
    tone = ("aevalsrc=0.45*sin(2*PI*196*t)*(0.55+0.45*sin(2*PI*0.9*t))"
            "*(0.5+0.5*abs(sin(2*PI*0.23*t))):s=48000:d=" + str(RECORDING_SECONDS))
    process = subprocess.Popen(
        [binary, "-y", "-loglevel", "error",
         "-f", "rawvideo", "-pix_fmt", "bgra", "-s", f"{width}x{height}", "-r", str(FPS), "-i", "-",
         "-f", "lavfi", "-i", tone,
         "-c:v", "libx264", "-preset", "fast", "-crf", "23", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-b:a", "96k", "-shortest", "-movflags", "+faststart", str(path)],
        stdin=subprocess.PIPE,
    )
    for i in range(RECORDING_SECONDS * FPS):
        frame = recorded_frame(i / FPS).convertToFormat(QImage.Format.Format_ARGB32)
        process.stdin.write(frame.constBits().asstring(frame.sizeInBytes()))
    process.stdin.close()
    if process.wait() != 0:
        raise RuntimeError("ffmpeg failed writing the demo recording")
    return path


# -- the player -------------------------------------------------------------


def open_player(path: Path):
    from snipux.player import PlayerWindow

    window = PlayerWindow(path)
    window.resize(1320, 860)
    window.show()
    wait_for(lambda: window.state.duration > 0, "the recording to load")
    wait_for(lambda: window._probes_started and window._filmstrip._done
             and (window._waveform is None or window.rail._peaks), "the filmstrip and waveform")
    return window


def seek(window, seconds: float) -> None:
    window.player.setPosition(int(seconds * 1000))
    wait_for(lambda: abs(window.state.position - seconds) < 0.1, "a seek")
    settle(150)


def trim(window, start: float, end: float, playhead: float) -> None:
    """Set the trim the way a person does: seek, then Start here / End here."""
    seek(window, start)
    window.mark_in()
    seek(window, end)
    window.mark_out()
    seek(window, playhead)


def wait_for(condition, what: str, seconds: float = 30) -> None:
    for _ in range(int(seconds * 20)):
        if condition():
            return
        settle(50)
    raise TimeoutError(f"timed out waiting for {what}")


def player_still(stage: Stage) -> QImage | None:
    path = make_recording(stage)
    if path is None:
        return None
    window = open_player(path)
    trim(window, 1.6, 10.2, 6.3)
    settle(400)
    image = on_desktop(compose(window), desk.build_scene())
    close_all()
    return image


# -- clips ----------------------------------------------------------------


def desktop_source(scene: desk.Scene):
    image = scene.image.copy()
    image.setDevicePixelRatio(SCALE)
    return lambda: image


def os_balloon(base: QImage, title: str, text: str, opacity: float) -> QImage:
    """The operating system's notification, which is where the tray message
    snipux sends after a Copy appears. Drawn here because no grab sees it."""
    image = base.copy()
    image.setDevicePixelRatio(SCALE)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setOpacity(opacity)
    try:
        card = QRectF(1600 - 380 - 16, 1000 - desk.TASKBAR_H - 96 - 16, 380, 96)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(0, 0, 0, 90))
        painter.drawRoundedRect(card.translated(0, 6), 12, 12)
        painter.setBrush(QColor("#22252d"))
        painter.setPen(QPen(QColor(255, 255, 255, 30), 1))
        painter.drawRoundedRect(card, 12, 12)
        logo = QImage(str(PACKAGE_DIR / "design" / "logo" / "snipux-64.png"))
        painter.drawImage(QRectF(card.left() + 18, card.top() + 24, 48, 48), logo)
        families = font_families()
        bold = QFont(families.ui)
        bold.setPixelSize(16)
        bold.setWeight(QFont.Weight.DemiBold)
        painter.setFont(bold)
        painter.setPen(QColor("#f1f3f7"))
        painter.drawText(QPointF(card.left() + 82, card.top() + 40), title)
        body = QFont(families.ui)
        body.setPixelSize(15)
        painter.setFont(body)
        painter.setPen(QColor("#b9bfcc"))
        painter.drawText(QPointF(card.left() + 82, card.top() + 66), text)
    finally:
        painter.end()
    return image


def clip_snip(stage: Stage, out: Path) -> list[Path]:
    """Shortcut, frozen screen, drag a region, box / steps / arrow, Copy."""
    scene = desk.build_scene()
    encoder = Encoder(out, "snip-and-annotate")
    reel = Reel(encoder, desktop_source(scene), QPointF(700, 760))
    reel.move(QPointF(980, 620), 1.0)
    reel.keys(["Ctrl", "Alt", "S"], 1.6)
    reel.hold(0.5)

    overlay = shots.open_overlay(scene)
    reel.source = lambda: compose(overlay)
    reel.hold(0.6)
    drag(reel, overlay, QPointF(888, 40), QPointF(1500, 476), 1.4)
    reel.hold(0.5)

    headline = scene.places["headline"].adjusted(-6, -6, 6, 6)
    tool_key(reel, overlay, "R")
    reel.hold(0.3)
    drag(reel, overlay, headline.topLeft(), headline.bottomRight(), 0.8)
    reel.hold(0.3)

    tool_key(reel, overlay, "S")
    bar = scene.places["best_bar"]
    tip = QPointF(bar.center().x() - 4, bar.top() - 14)
    for at in (QPointF(headline.right() + 4, headline.top() + 2), QPointF(tip.x() + 2, tip.y() - 30)):
        reel.move(at, 0.6)
        press(overlay, at)
        reel.click()
        reel.hold(0.3)

    tool_key(reel, overlay, "A")
    drag(reel, overlay, QPointF(tip.x() - 250, tip.y() + 150), tip, 0.9)
    reel.hold(0.6)

    copy = overlay._bar._action
    reel.move(QPointF(copy.mapTo(overlay, QPoint(28, copy.height() // 2))), 0.8)
    reel.click()
    reel.hold(0.2)
    # The Copy button's own path: copy, then dismiss.
    overlay._on_bar_copy()
    after = scene.image.copy()
    reel.source = lambda: os_balloon(after, "Snipux", "Copied to clipboard", 1.0)
    reel.hold(2.2)
    close_all()
    return encoder.close()


def clip_hide_sensitive(stage: Stage, out: Path) -> list[Path]:
    """Arm Hide sensitive with one click, frame the chat, watch it black out."""
    scene = desk.build_scene()
    encoder = Encoder(out, "hide-sensitive")
    overlay = shots.open_overlay_with_ocr(scene)
    overlay._chooser.set_hide_sensitive(False)
    reel = Reel(encoder, lambda: compose(overlay), QPointF(1200, 700))
    reel.hold(0.8)
    flag = overlay._chooser.row.hide_flag
    reel.hold(0.4)
    click_widget(reel, flag, overlay, 1.0)
    reel.hold(1.0)
    chat = desk.CHAT
    drag(reel, overlay, QPointF(chat.left() - 16, chat.top() - 16),
         QPointF(chat.right() + 16, chat.bottom() + 16), 1.5)
    reel.move(QPointF(1300, 560), 0.8)
    reel.hold(3.0)
    close_all()
    return encoder.close()


def clip_record(stage: Stage, out: Path) -> list[Path]:
    """Frame the editor, Record, the recording runs, then trim and export."""
    path = make_recording(stage)
    if path is None:
        return []
    scene = desk.build_scene()
    encoder = Encoder(out, "record-and-trim")
    shots.recordings_open_in_the_player()
    overlay = shots.open_overlay(scene)
    reel = Reel(encoder, lambda: compose(overlay), QPointF(1100, 700))
    reel.hold(0.4)
    click_widget(reel, overlay._chooser.row.record, overlay, 0.7)
    reel.hold(0.5)
    region = QRectF(RECORD_SELECTION)
    drag(reel, overlay, region.topLeft(), region.bottomRight(), 1.3)
    bar = shots.place_recording_bar(overlay, region)
    reel.hold(0.7)
    reel.move(centre_in(bar._action, overlay), 0.7)
    reel.click()
    reel.hold(0.3)

    # Recording: the overlay is gone, the desktop is live again, and the
    # bar counts. The region's red frame is drawn as RegionFrame draws it.
    close_all()
    live = place_live_bar(region)
    red = design.flow_color("REC")
    # Four seconds of a six-second stretch of typing, sped up to keep it short.
    for i in range(int(4 * FPS)):
        live.set_live(f"00:{int(i / FPS):02d}")
        frame = with_widget(recording_desktop(2.0 + i / FPS * 6.0 / 4, region, red), live)
        reel.source = lambda frame=frame: frame
        reel.hold(1 / FPS)
    frame = with_widget(recording_desktop(8.0, region, red), live)
    reel.source = lambda: frame
    reel.move(centre_in(live._action, live) + QPointF(live.geometry().topLeft()), 0.6)
    reel.click()
    reel.hold(0.3)
    live.close()

    window = open_player(path)
    seek(window, 0.0)
    reel.source = lambda: on_desktop(compose(window), scene)
    reel.hold(0.5)
    canvas_origin = player_origin(window, scene)
    for at, button in ((1.6, window.mark_in_button), (10.2, window.mark_out_button)):
        seek(window, at)
        reel.hold(0.3)
        reel.move(canvas_origin + centre_in(button, window), 0.7)
        reel.click()
        button.clicked.emit()
        reel.hold(0.6)
    export = window.export_button
    reel.move(canvas_origin + QPointF(export.mapTo(window, QPoint(export.width() // 3, export.height() // 2))), 0.8)
    reel.click()
    window.run_export()
    reel.hold(1.8)
    close_all()
    return encoder.close()


def recording_desktop(t: float, region: QRectF, red: QColor) -> QImage:
    image = desk.build_scene(typed=typed_at(t)).image.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
    image.setDevicePixelRatio(SCALE)
    painter = QPainter(image)
    try:
        painter.setPen(QPen(red, 3, Qt.PenStyle.SolidLine, Qt.PenCapStyle.SquareCap, Qt.PenJoinStyle.MiterJoin))
        painter.drawRect(region.adjusted(-1.5, -1.5, 1.5, 1.5))
    finally:
        painter.end()
    return image


def place_live_bar(region: QRectF):
    """The live bar as a window of its own, where app.py places it once the
    overlay has closed and recording has begun."""
    from snipux.app import _place_recording_hud
    from snipux.flowbars import RecordingBar

    bar = RecordingBar()
    bar.set_live("00:00")
    bar.adjustSize()
    spot = _place_recording_hud(region, [QRectF(0, 0, 1600, 1000)], bar.sizeHint())
    bar.move(QRectF(spot).center().toPoint() - bar.rect().center())
    bar.show()
    settle(100)
    return bar


def with_widget(base: QImage, widget: QWidget) -> QImage:
    """`base` (the desktop, 2x) with a top-level `widget` painted where it sits."""
    image = base.copy()
    image.setDevicePixelRatio(SCALE)
    painter = QPainter(image)
    try:
        painter.drawPixmap(widget.geometry().topLeft(), widget.grab())
    finally:
        painter.end()
    return image


def player_origin(window, scene: desk.Scene) -> QPointF:
    """Where `on_desktop` puts the window, so the pointer can aim at it."""
    w, h = window.width(), window.height()
    area_h = scene.logical_size.height() - desk.TASKBAR_H
    return QPointF((scene.logical_size.width() - w) / 2, max(16.0, (area_h - h) / 2))


CLIPS = {
    "snip-and-annotate": clip_snip,
    "hide-sensitive": clip_hide_sensitive,
    "record-and-trim": clip_record,
}


def render_all(stage: Stage, out: Path, names: list[str] | None = None) -> None:
    for name in names or list(CLIPS):
        for path in CLIPS[name](stage, out):
            print(f"wrote video/{path.name} ({path.stat().st_size / 1e6:.1f} MB)")
