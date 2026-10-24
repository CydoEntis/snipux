"""Frame-by-frame video of real widgets, and the encoder it feeds.

A `Reel` asks for one picture per frame at 30 fps, paints the things a grab
cannot see -- the pointer, a click ripple, the keys being pressed -- and hands
the frame to an `Encoder`, which pipes it to the system ffmpeg or, where
there is none, writes it out as a PNG sequence.
"""

from __future__ import annotations

import math
import shutil
import subprocess
import time
from pathlib import Path
from typing import Callable

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import QColor, QFont, QFontMetricsF, QImage, QPainter, QPainterPath, QPen

from snipux.design import font_families

FPS = 30
# Output size of every clip: the 1600x1000 logical desktop at 1x.
SIZE = (1600, 1000)


def ffmpeg_binary() -> str | None:
    return shutil.which("ffmpeg")


class Encoder:
    """Collect frames; on `close`, make the MP4, WebM and GIF from them."""

    def __init__(self, out_dir: Path, name: str, size=SIZE):
        self._out_dir = out_dir
        self._name = name
        self._size = size
        self._binary = ffmpeg_binary()
        self._count = 0
        out_dir.mkdir(parents=True, exist_ok=True)
        if self._binary is None:
            self._frames = out_dir / "frames" / name
            self._frames.mkdir(parents=True, exist_ok=True)
            self._process = None
            return
        self._process = subprocess.Popen(
            [self._binary, "-y", "-loglevel", "error",
             "-f", "rawvideo", "-pix_fmt", "bgra", "-s", f"{size[0]}x{size[1]}", "-r", str(FPS),
             "-i", "-",
             "-c:v", "libx264", "-preset", "slow", "-crf", "20", "-pix_fmt", "yuv420p",
             "-movflags", "+faststart", str(self.mp4)],
            stdin=subprocess.PIPE,
        )

    @property
    def mp4(self) -> Path:
        return self._out_dir / f"{self._name}.mp4"

    def write(self, frame: QImage) -> None:
        frame = frame.convertToFormat(QImage.Format.Format_ARGB32)
        self._count += 1
        if self._process is None:
            frame.save(str(self._frames / f"{self._count:04d}.png"), "PNG")
            return
        self._process.stdin.write(frame.constBits().asstring(frame.sizeInBytes()))

    def close(self) -> list[Path]:
        if self._process is None:
            print(f"ffmpeg not found: left {self._count} PNG frames in {self._frames}")
            return []
        self._process.stdin.close()
        if self._process.wait() != 0:
            raise RuntimeError(f"ffmpeg failed encoding {self.mp4}")
        webm = self._out_dir / f"{self._name}.webm"
        gif = self._out_dir / f"{self._name}.gif"
        run(self._binary, "-i", str(self.mp4), "-c:v", "libvpx-vp9", "-b:v", "0", "-crf", "36",
            "-row-mt", "1", "-deadline", "good", "-cpu-used", "4", "-an", str(webm))
        # A palette made for this clip, then dithered onto it: the default
        # 256-colour web palette bands every gradient on the wallpaper.
        run(self._binary, "-i", str(self.mp4), "-filter_complex",
            "fps=12,scale=720:-1:flags=lanczos,split[a][b];"
            "[a]palettegen=max_colors=192:stats_mode=diff[p];"
            "[b][p]paletteuse=dither=sierra2_4a:diff_mode=rectangle",
            str(gif))
        return [self.mp4, webm, gif]


def run(binary: str, *args: str) -> None:
    subprocess.run([binary, "-y", "-loglevel", "error", *args], check=True)


def ease(t: float) -> float:
    """Ease in and out, so the pointer starts and lands softly."""
    t = max(0.0, min(1.0, t))
    return t * t * (3 - 2 * t)


class Reel:
    """Drives a clip in frames. `source()` returns the screen as it is now,
    a 2x image of the 1600x1000 desktop; the reel adds the pointer and keys.
    """

    def __init__(self, encoder: Encoder, source: Callable[[], QImage], pointer: QPointF):
        self.encoder = encoder
        self.source = source
        self.pointer = QPointF(pointer)
        self.frame = 0
        self._keys: list[tuple[int, int, list[str]]] = []
        self._ripples: list[tuple[int, QPointF]] = []
        self.pointer_visible = True
        self._last = time.monotonic()

    def hold(self, seconds: float, each: Callable[[float], None] | None = None) -> None:
        frames = max(1, round(seconds * FPS))
        for i in range(frames):
            if each is not None:
                each(i / max(1, frames - 1))
            self._shoot()

    def move(self, to: QPointF, seconds: float, each: Callable[[QPointF], None] | None = None) -> None:
        start = QPointF(self.pointer)
        frames = max(1, round(seconds * FPS))
        for i in range(1, frames + 1):
            k = ease(i / frames)
            # A slight arc reads as a hand moving a mouse, not a tween.
            lift = math.sin(k * math.pi) * min(40.0, (to - start).manhattanLength() * 0.06)
            self.pointer = QPointF(start.x() + (to.x() - start.x()) * k,
                                   start.y() + (to.y() - start.y()) * k - lift)
            if each is not None:
                each(QPointF(self.pointer))
            self._shoot()

    def click(self) -> None:
        self._ripples.append((self.frame, QPointF(self.pointer)))

    def keys(self, labels: list[str], seconds: float = 1.4) -> None:
        self._keys.append((self.frame, self.frame + round(seconds * FPS), labels))

    def _shoot(self) -> None:
        from .shots import settle

        # Paced to the frame rate, so the widgets' own timers (a toast's
        # life, a fade) run about as long as they do for a person.
        now = time.monotonic()
        settle(max(1, round((1 / FPS - (now - self._last)) * 1000)))
        self._last = time.monotonic()
        screen = self.source().convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
        screen.setDevicePixelRatio(1.0)
        frame = screen.scaled(SIZE[0], SIZE[1], Qt.AspectRatioMode.IgnoreAspectRatio,
                              Qt.TransformationMode.SmoothTransformation)
        painter = QPainter(frame)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        try:
            self._paint_ripples(painter)
            if self.pointer_visible:
                paint_pointer(painter, self.pointer)
            self._paint_keys(painter)
        finally:
            painter.end()
        self.encoder.write(frame)
        self.frame += 1

    def _paint_ripples(self, painter: QPainter) -> None:
        for start, at in self._ripples:
            age = (self.frame - start) / (FPS * 0.45)
            if 0 <= age <= 1:
                painter.setPen(QPen(QColor(255, 255, 255, int(200 * (1 - age))), 2.5))
                painter.setBrush(QColor(255, 255, 255, int(60 * (1 - age))))
                radius = 6 + 22 * ease(age)
                painter.drawEllipse(at, radius, radius)

    def _paint_keys(self, painter: QPainter) -> None:
        for start, end, labels in self._keys:
            if not start <= self.frame < end:
                continue
            fade = min(1.0, (self.frame - start) / 5, (end - self.frame) / 6)
            paint_keycaps(painter, labels, fade)


def paint_pointer(painter: QPainter, at: QPointF) -> None:
    """A standard arrow pointer, white with a dark edge and a soft shadow."""
    path = QPainterPath(QPointF(0, 0))
    for x, y in ((0, 22), (5.5, 17), (9.5, 26), (13, 24.5), (9, 15.8), (16, 15.8)):
        path.lineTo(x, y)
    path.closeSubpath()
    painter.save()
    painter.translate(at)
    painter.translate(1.5, 2.5)
    painter.fillPath(path, QColor(0, 0, 0, 70))
    painter.translate(-1.5, -2.5)
    painter.setPen(QPen(QColor("#111318"), 1.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap,
                        Qt.PenJoinStyle.RoundJoin))
    painter.setBrush(QColor("#ffffff"))
    painter.drawPath(path)
    painter.restore()


def paint_keycaps(painter: QPainter, labels: list[str], opacity: float) -> None:
    """The keys being pressed, as keycaps low and centred on the frame."""
    font = QFont(font_families().ui)
    font.setPixelSize(26)
    font.setWeight(QFont.Weight.DemiBold)
    metrics = QFontMetricsF(font)
    widths = [max(58.0, metrics.horizontalAdvance(label) + 36) for label in labels]
    plus = 30.0
    total = sum(widths) + plus * (len(labels) - 1)
    x = (SIZE[0] - total) / 2
    y = SIZE[1] - 190.0
    painter.save()
    painter.setOpacity(opacity)
    painter.setFont(font)
    for i, (label, width) in enumerate(zip(labels, widths)):
        cap = QRectF(x, y, width, 58)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(0, 0, 0, 120))
        painter.drawRoundedRect(cap.translated(0, 5), 12, 12)
        painter.setBrush(QColor("#2a2e37"))
        painter.setPen(QPen(QColor(255, 255, 255, 50), 1.2))
        painter.drawRoundedRect(cap, 12, 12)
        painter.setPen(QColor("#f1f3f7"))
        painter.drawText(cap, Qt.AlignmentFlag.AlignCenter, label)
        x += width
        if i < len(labels) - 1:
            painter.setPen(QColor(255, 255, 255, 200))
            painter.drawText(QRectF(x, y, plus, 58), Qt.AlignmentFlag.AlignCenter, "+")
            x += plus
    painter.restore()
