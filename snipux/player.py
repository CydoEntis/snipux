"""The recording player / trim editor -- `docs/design/player` (LOCKED 2026-08-27).

The video counterpart of `review.py`, and the window a recording opens into
when Record mode's destination is `Open`. It does four things and
deliberately nothing else: play a recording back, trim it, drop the audio,
export it.

Chrome is the review window's, not the overlay's glass, so this builds on
`WinWindow` and only the middle of the window is new: a canvas with a
floating transport over it, and a timeline panel under it.

Three decisions worth knowing before reading:

**The frame is painted, not surfaced.** `QVideoWidget` is a native video
surface, and the design floats a transport bar, two corner badges and a
pause overlay *on top of* the picture; widgets stacked over a native
surface do not composite reliably. Feeding a `QVideoSink` and painting the
frame ourselves in `paintEvent` puts the video in the ordinary widget
stack, where the zoom transform, the 28% pause dim and the play badge are
just more drawing.

**Nothing is decoded during a paint.** The filmstrip's thumbnails and the
waveform's peaks are each read once when the file opens, off their own
media objects, and cached; the rail paints whatever has arrived so far and
placeholders for the rest. Decoding on paint would make scrubbing -- the
one interaction this window exists for -- stutter.

**`muted` drops the audio track on export.** It is not `setVolume(0)`.
Those are two different promises and the greyed-out waveform is making the
first one, so the export path must honour it.
"""

from __future__ import annotations

import shutil
import struct
import subprocess
from dataclasses import dataclass
from pathlib import Path

from PyQt6.QtCore import (
    QObject,
    QPointF,
    QRect,
    QRectF,
    QSize,
    Qt,
    QTimer,
    QUrl,
    pyqtSignal,
)
from PyQt6.QtGui import (
    QColor,
    QFontMetrics,
    QGuiApplication,
    QImage,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPixmap,
)
from PyQt6.QtMultimedia import (
    QAudioDecoder,
    QAudioFormat,
    QAudioOutput,
    QMediaPlayer,
    QVideoSink,
)
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout, QWidget

from . import design
from .design import tokens
from .flowbars import FlowMenu
from .winchrome import WinWindow, _mono_font, _ui_font

_M = tokens.PlayerMetric
_C = tokens.PlayerColor


def _rgba(hex_colour: str, alpha: float) -> QColor:
    """`hex_colour` at `alpha` (0..1).

    The handoff writes these as `rgba(255,255,255,.12)`; the tokens keep the
    hex and the alpha separately so a colour is named once, and this is
    where the two are put back together.
    """
    colour = QColor(hex_colour)
    colour.setAlphaF(alpha)
    return colour


def format_timecode(seconds: float, *, fps: int = tokens.PLAYER_FPS) -> str:
    """`mm:ss.ff` -- the frame-accurate form, for the playhead and in/out.

    Frames rather than hundredths because a trim lands on a frame boundary:
    "00:12.18" is a real position in the file, "00:12.60" would not be.
    """
    seconds = max(0.0, seconds)
    minutes = int(seconds // 60)
    whole = int(seconds % 60)
    frames = min(int(round((seconds % 1) * fps)), fps - 1)
    return f"{minutes:02d}:{whole:02d}.{frames:02d}"


def format_clock(seconds: float) -> str:
    """`mm:ss` -- durations, which are read at a glance rather than aimed at."""
    seconds = max(0.0, seconds)
    return f"{int(seconds // 60):02d}:{int(seconds % 60):02d}"


def estimate_size_mb(format_id: str, trimmed_seconds: float) -> float:
    """Rough export size for `format_id` over the trimmed range, in MB.

    A live figure next to each menu row is the point: GIF's "big above ~10
    seconds" is a sentence, "31.2 MB" is the answer. The per-second rates
    are the handoff's and are deliberately approximate -- a real encode
    depends on the content.
    """
    for fid, _glyph, _label, _note, rate in tokens.EXPORT_FORMATS:
        if fid != format_id:
            continue
        if rate is None:
            return tokens.EXPORT_FRAME_MB
        return round(trimmed_seconds * rate, 1)
    return 0.0


# --------------------------------------------------------------------------
# Reading the file: thumbnails and peaks, each once, off to the side.
# --------------------------------------------------------------------------


class FilmstripProbe(QObject):
    """Decodes `cells` evenly-spaced thumbnails from a video, one at a time.

    Its own `QMediaPlayer`, paused, with no audio output: seeking a paused
    player makes the ffmpeg backend deliver exactly one frame for the seek
    target, which is the cheapest frame grab Qt offers and lands within a
    frame of where it was asked (measured: 200ms asked, 166ms delivered).

    One at a time, chained through the frame signal rather than fired as a
    batch, because a burst of seeks coalesces -- the backend is free to drop
    an outstanding seek when a newer one arrives, and the strip comes back
    with holes.

    Failure is silent by design: a cell that never arrives stays a flat tone
    in the rail. A thumbnail is decoration on a control that works without
    it, and a codec this build cannot decode must not stop the window from
    opening.
    """

    cell_ready = pyqtSignal(int, QImage)
    finished = pyqtSignal()

    def __init__(self, path: Path, duration_s: float, cells: int, parent=None):
        super().__init__(parent)
        self._duration_s = duration_s
        self._cells = cells
        self._wanted: int | None = None
        self._done = False

        self._sink = QVideoSink(self)
        self._player = QMediaPlayer(self)
        self._player.setVideoSink(self._sink)
        self._sink.videoFrameChanged.connect(self._on_frame)
        self._player.mediaStatusChanged.connect(self._on_status)
        self._player.errorOccurred.connect(lambda *_: self._finish())
        self._player.setSource(QUrl.fromLocalFile(str(path)))

    def _target_ms(self, index: int) -> int:
        # Mid-cell rather than cell-start: a cell shows what is in the
        # middle of the slice it represents, and the frame at t=0 of a
        # screen recording is often the desktop before anything happened.
        fraction = (index + 0.5) / self._cells
        return int(fraction * self._duration_s * 1000)

    def _on_status(self, status) -> None:
        if self._done or self._wanted is not None:
            return
        if status == QMediaPlayer.MediaStatus.LoadedMedia:
            self._player.pause()
            self._request(0)

    def _request(self, index: int) -> None:
        self._wanted = index
        self._player.setPosition(self._target_ms(index))

    def _on_frame(self, frame) -> None:
        index = self._wanted
        if self._done or index is None or not frame.isValid():
            return
        image = frame.toImage()
        self._wanted = None
        if not image.isNull():
            self.cell_ready.emit(index, image.copy())
        if index + 1 < self._cells:
            QTimer.singleShot(0, lambda: self._request(index + 1))
        else:
            self._finish()

    def _finish(self) -> None:
        if self._done:
            return
        self._done = True
        self._player.stop()
        self._player.setSource(QUrl())
        self.finished.emit()


class WaveformProbe(QObject):
    """Decodes a file's audio down to `bars` peak values in 0..1.

    Forces the decoder to mono float at a low sample rate: the waveform is
    120 bars tall-or-short, so decoding at the source's 44.1kHz stereo would
    read forty times the data to answer the same question, and the mixdown
    Qt does for us is exactly the one a single-row waveform wants.

    Emits an empty list when the file has no audio track or the decode
    fails, which the rail reads as "hide the band" rather than "draw a flat
    line" -- a flat line looks like silence, and silence is not the same
    fact as no audio at all.
    """

    ready = pyqtSignal(list)

    _SAMPLE_RATE = 8000

    def __init__(self, path: Path, duration_s: float, bars: int, parent=None):
        super().__init__(parent)
        self._duration_s = max(duration_s, 0.001)
        self._bars = bars
        self._peaks: list[tuple[float, float]] = []
        self._done = False

        audio_format = QAudioFormat()
        audio_format.setSampleFormat(QAudioFormat.SampleFormat.Float)
        audio_format.setSampleRate(self._SAMPLE_RATE)
        audio_format.setChannelCount(1)

        self._decoder = QAudioDecoder(self)
        self._decoder.setAudioFormat(audio_format)
        self._decoder.bufferReady.connect(self._drain)
        self._decoder.finished.connect(self._emit)
        self._decoder.error.connect(lambda *_: self._emit(empty=True))
        self._decoder.setSource(QUrl.fromLocalFile(str(path)))
        self._decoder.start()

    def _drain(self) -> None:
        while self._decoder.bufferAvailable():
            buffer = self._decoder.read()
            frames = buffer.frameCount()
            if frames <= 0:
                continue
            raw = buffer.constData().asstring(buffer.byteCount())
            try:
                values = struct.unpack(f"<{frames}f", raw)
            except struct.error:
                continue
            start_s = buffer.startTime() / 1_000_000
            self._peaks.append((start_s, max(abs(v) for v in values)))

    def _emit(self, empty: bool = False) -> None:
        if self._done:
            return
        self._done = True
        self._decoder.stop()
        self.ready.emit([] if empty else self._bucket())

    def _bucket(self) -> list[float]:
        """Fold the per-buffer peaks into exactly `bars` values.

        The decoder hands back a few hundred short buffers (measured: 259
        over six seconds), so bucketing by time gives every bar several
        samples to take a maximum over. Normalised against the loudest bar
        rather than 1.0 -- a quiet recording should still show a shape.
        """
        if not self._peaks:
            return []
        buckets = [0.0] * self._bars
        for start_s, peak in self._peaks:
            index = int(start_s / self._duration_s * self._bars)
            index = max(0, min(self._bars - 1, index))
            buckets[index] = max(buckets[index], peak)
        loudest = max(buckets)
        if loudest <= 0:
            return []
        return [value / loudest for value in buckets]


# --------------------------------------------------------------------------
# The timeline
# --------------------------------------------------------------------------


@dataclass
class TrimState:
    """What the rail draws and the export reads.

    `duration` is the source's; `start`/`end` are the range being kept.
    Kept as one object because every readout in the window -- the canvas
    badge, the transport's time, the trim row's sentence, the export
    estimate -- is a different phrasing of these three numbers, and they
    must never disagree.
    """

    duration: float = 0.0
    start: float = 0.0
    end: float = 0.0
    position: float = 0.0

    @property
    def kept(self) -> float:
        return max(0.0, self.end - self.start)

    @property
    def cut(self) -> float:
        return max(0.0, self.duration - self.kept)

    @property
    def trimmed(self) -> bool:
        # A hair of slack: dragging a handle to the very end lands within a
        # rounding error of the duration, and calling that "trimmed" would
        # claim a cut nobody made.
        return self.kept < self.duration - 0.05

    @property
    def cut_is_reportable(self) -> bool:
        """Whether the cut figure is worth printing.

        `cut` is shown as mm:ss, so anything under a second renders as
        "−00:00 cut" -- a clause that says a cut happened and then reports
        nothing, which reads as a bug. Below a second the trim is real but
        not sayable at this precision, so the clause is left off.
        """
        return self.trimmed and self.cut >= 1.0


class _RailHandle(QWidget):
    """One of the two trim grips.

    A child widget rather than something the rail paints and hit-tests
    itself, so it can carry its own `ew-resize` cursor and its own 14px hit
    area -- 8px of visible bar is not enough to grab.

    Every one of its mouse handlers hands the *global* position back to the
    rail. Deriving the fraction from a position local to this widget is the
    bug the handoff warns about: this widget is 14px wide, so every drag
    would resolve to 0 or 1 and the handle would teleport to whichever end
    it was nearest.
    """

    def __init__(self, kind: str, rail: "TimelineRail"):
        super().__init__(rail)
        self._kind = kind
        self._rail = rail
        self.setFixedWidth(_M.HANDLE_HIT_W)
        self.setCursor(Qt.CursorShape.SizeHorCursor)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        bar = QRectF(
            (self.width() - _M.HANDLE_W) / 2,
            (self.height() - _M.HANDLE_H) / 2,
            _M.HANDLE_W,
            _M.HANDLE_H,
        )
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(_C.TRIM))
        painter.drawRoundedRect(bar, _M.HANDLE_RADIUS, _M.HANDLE_RADIUS)
        grip = QRectF(bar.center().x() - 1, bar.center().y() - 7, 2, 14)
        painter.setBrush(_rgba(_C.HANDLE_GRIP, 0.5))
        painter.drawRect(grip)
        painter.end()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        self._rail.begin_drag(self._kind, event.globalPosition())
        event.accept()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        self._rail.drag_to(event.globalPosition())
        event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        self._rail.end_drag()
        event.accept()


class TimelineRail(QWidget):
    """Ruler, filmstrip, waveform, veil, range edges and playhead in one paint.

    One widget rather than four stacked ones because they share a single
    horizontal mapping -- a pixel is a time, everywhere in here -- and four
    widgets would each have to be told the same three numbers and kept in
    step. The two handles are the exception, above.
    """

    trim_changed = pyqtSignal()
    scrubbed = pyqtSignal(float)

    def __init__(self, state: TrimState, parent: QWidget | None = None):
        super().__init__(parent)
        self.state = state
        self.setFixedHeight(_M.RAIL_H)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMouseTracking(True)

        self._thumbs: dict[int, QPixmap] = {}
        self._peaks: list[float] = []
        self._muted = False
        self._drag: str | None = None

        self._handle_in = _RailHandle("in", self)
        self._handle_out = _RailHandle("out", self)

    # -- what the probes deliver ---------------------------------------

    def set_thumbnail(self, index: int, image: QImage) -> None:
        cell = self._cell_rect(index)
        if cell.width() < 1 or cell.height() < 1:
            # Laid out at zero width (the window has not been shown yet):
            # keep the image at a size the first real paint can scale from
            # rather than dropping it.
            self._thumbs[index] = QPixmap.fromImage(image)
        else:
            self._thumbs[index] = QPixmap.fromImage(
                image.scaled(
                    cell.size().toSize(),
                    Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
        self.update()

    def set_peaks(self, peaks: list[float]) -> None:
        self._peaks = peaks
        self.update()

    def set_muted(self, muted: bool) -> None:
        self._muted = muted
        self.update()

    @property
    def has_audio(self) -> bool:
        return bool(self._peaks)

    # -- geometry -------------------------------------------------------

    def _film_top(self) -> int:
        return _M.RULER_H

    def _film_height(self) -> int:
        # With no audio track the filmstrip takes the waveform's height
        # rather than leaving a dead band -- there is nothing to say there.
        if self.has_audio:
            return _M.FILMSTRIP_H
        return self.height() - _M.RULER_H

    def _wave_top(self) -> int:
        return _M.RULER_H + _M.FILMSTRIP_H

    def _cell_rect(self, index: int) -> QRectF:
        cells = _M.FILMSTRIP_CELLS
        width = self.width() / cells
        return QRectF(index * width, self._film_top(), width, self._film_height())

    def x_for(self, seconds: float) -> float:
        if self.state.duration <= 0:
            return 0.0
        return seconds / self.state.duration * self.width()

    def time_at(self, x: float) -> float:
        if self.width() <= 0:
            return 0.0
        fraction = max(0.0, min(1.0, x / self.width()))
        return fraction * self.state.duration

    # -- dragging -------------------------------------------------------

    def begin_drag(self, kind: str, global_pos: QPointF) -> None:
        self._drag = kind
        self.drag_to(global_pos)

    def drag_to(self, global_pos: QPointF) -> None:
        if self._drag is None:
            return
        # Against the rail, always -- never against whatever widget the
        # press landed on. See _RailHandle's docstring.
        local_x = self.mapFromGlobal(global_pos.toPoint()).x()
        seconds = self.time_at(local_x)
        state = self.state
        floor = _M.MIN_RANGE_S

        if self._drag == "in":
            state.start = max(0.0, min(seconds, state.end - floor))
            state.position = state.start
            self.trim_changed.emit()
        elif self._drag == "out":
            state.end = min(state.duration, max(seconds, state.start + floor))
            state.position = state.end
            self.trim_changed.emit()
        else:
            state.position = seconds
            self.scrubbed.emit(seconds)
        self.sync()

    def end_drag(self) -> None:
        self._drag = None

    def mousePressEvent(self, event: QMouseEvent) -> None:
        self.begin_drag("now", event.globalPosition())
        event.accept()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        self.drag_to(event.globalPosition())
        event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        self.end_drag()
        event.accept()

    # -- layout ---------------------------------------------------------

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self.sync()

    def sync(self) -> None:
        """Put the handles where the state says, and repaint."""
        top = self._film_top()
        height = self.height() - top
        for handle, seconds in (
            (self._handle_in, self.state.start),
            (self._handle_out, self.state.end),
        ):
            x = round(self.x_for(seconds) - _M.HANDLE_HIT_W / 2)
            handle.setGeometry(x, top, _M.HANDLE_HIT_W, height)
            handle.raise_()
        self.update()

    # -- paint ----------------------------------------------------------

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        body = QRectF(0.5, 0.5, self.width() - 1, self.height() - 1)
        clip = QPainterPath()
        clip.addRoundedRect(body, _M.RAIL_RADIUS, _M.RAIL_RADIUS)
        painter.setClipPath(clip)

        painter.fillRect(self.rect(), QColor(_C.RAIL_BG))
        self._paint_filmstrip(painter)
        if self.has_audio:
            self._paint_waveform(painter)
        self._paint_ruler(painter)
        self._paint_range(painter)
        self._paint_playhead(painter)

        painter.setClipping(False)
        painter.setPen(QColor(_C.RAIL_BORDER))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(body, _M.RAIL_RADIUS, _M.RAIL_RADIUS)
        painter.end()

    def _paint_ruler(self, painter: QPainter) -> None:
        painter.setPen(QColor(_C.RULER_RULE))
        painter.drawLine(0, _M.RULER_H, self.width(), _M.RULER_H)
        if self.state.duration <= 0:
            return

        font = _mono_font(9)
        painter.setFont(font)
        second = 0
        while second <= self.state.duration:
            x = self.x_for(second)
            painter.setPen(QColor(_C.TICK))
            painter.drawLine(int(x), _M.RULER_H - 5, int(x), _M.RULER_H)
            painter.setPen(QColor(_C.TICK_FG))
            painter.drawText(
                QRectF(x + 3, 0, 44, _M.RULER_H - 5),
                int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                format_clock(second),
            )
            second += _M.TICK_EVERY_S

    def _paint_filmstrip(self, painter: QPainter) -> None:
        top = self._film_top()
        height = self._film_height()
        for index in range(_M.FILMSTRIP_CELLS):
            cell = self._cell_rect(index)
            centre = self.time_at(cell.center().x())
            inside = self.state.start <= centre <= self.state.end
            # Outside cells dim rather than vanish: the trim has to read at
            # a glance, but what was cut still has to be findable to be
            # dragged back in.
            painter.setOpacity(1.0 if inside else _C.OUTSIDE_OPACITY)

            thumb = self._thumbs.get(index)
            if thumb is not None and not thumb.isNull():
                painter.save()
                painter.setClipRect(cell.toRect())
                target = QRectF(thumb.rect())
                target.moveCenter(cell.center())
                painter.drawPixmap(target.topLeft(), thumb)
                painter.restore()
            else:
                painter.fillRect(cell, QColor(_C.FILM_CELL))

            painter.setPen(_rgba(_C.FILM_SEAM, 0.35))
            painter.drawLine(
                int(cell.right()), top, int(cell.right()), top + height
            )
        painter.setOpacity(1.0)

    def _paint_waveform(self, painter: QPainter) -> None:
        top = self._wave_top()
        height = self.height() - top
        bars = len(self._peaks)
        if bars == 0 or height <= 0:
            return
        width = self.width() / bars
        painter.setPen(Qt.PenStyle.NoPen)
        for index, peak in enumerate(self._peaks):
            centre = self.time_at((index + 0.5) * width)
            inside = self.state.start <= centre <= self.state.end
            if self._muted:
                colour = _C.WAVE_MUTED_IN if inside else _C.WAVE_MUTED_OUT
            else:
                colour = _C.WAVE_IN if inside else _C.WAVE_OUT
            bar_h = max(2.0, peak * (height - 4))
            painter.setBrush(QColor(colour))
            painter.drawRoundedRect(
                QRectF(
                    index * width + 0.5,
                    top + (height - bar_h) / 2,
                    max(1.0, width - 1),
                    bar_h,
                ),
                1,
                1,
            )

    def _paint_range(self, painter: QPainter) -> None:
        top = _M.RULER_H
        height = self.height() - top
        left = self.x_for(self.state.start)
        right = self.x_for(self.state.end)
        veil = _rgba(_C.OUTSIDE_VEIL, 0.72)
        painter.fillRect(QRectF(0, top, left, height), veil)
        painter.fillRect(QRectF(right, top, self.width() - right, height), veil)

        painter.setBrush(Qt.BrushStyle.NoBrush)
        for x in (left, right):
            painter.fillRect(
                QRectF(x - _M.RANGE_EDGE_W / 2, top, _M.RANGE_EDGE_W, height),
                QColor(_C.TRIM),
            )
        painter.setPen(_rgba(_C.TRIM_INNER, 0.18))
        painter.drawRect(
            QRectF(left + 1, top + 0.5, max(0.0, right - left - 2), height - 1)
        )

    def _paint_playhead(self, painter: QPainter) -> None:
        x = self.x_for(self.state.position)
        painter.fillRect(
            QRectF(x - _M.PLAYHEAD_W / 2, 0, _M.PLAYHEAD_W, self.height()),
            QColor(_C.PLAYHEAD),
        )

        label = format_timecode(self.state.position)
        font = _mono_font(9.5, 600)
        metrics = QFontMetrics(font)
        width = metrics.horizontalAdvance(label) + 10
        # Kept inside the rail: at either end the flag would otherwise hang
        # off the edge and be clipped by the rounded corner.
        flag_x = max(0.0, min(self.width() - width, x - width / 2))
        flag = QRectF(flag_x, 0, width, 14)

        path = QPainterPath()
        path.addRoundedRect(flag, 4, 4)
        painter.fillPath(path, QColor(_C.PLAYHEAD))
        painter.setFont(font)
        painter.setPen(QColor(_C.PLAYHEAD_FG))
        painter.drawText(flag, int(Qt.AlignmentFlag.AlignCenter), label)


# --------------------------------------------------------------------------
# The floating transport
# --------------------------------------------------------------------------


class _BarButton(QWidget):
    """A 28px control in the transport shell.

    Painted rather than styled for the same reason the flow bars are: this
    sits on a translucent panel, and a `QPushButton` stylesheet cannot give
    it a background that composites over what is behind the bar without
    also fighting `WA_TranslucentBackground`.

    `tone` is the whole state model -- "idle", "on" (the pre-lit play
    button), "accent" (loop and a non-1x speed) or "danger" (muted). Each
    is a background and a foreground; nothing else about the button
    changes.
    """

    clicked = pyqtSignal()

    def __init__(
        self,
        glyph: str | None,
        *,
        text: str = "",
        tone: str = "idle",
        rotate: int = 0,
        caret: bool = False,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self._glyph = glyph
        self._text = text
        self._tone = tone
        self._rotate = rotate
        self._caret = caret
        self._hover = False
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedHeight(_M.BTN)
        self.setFixedWidth(self._natural_width())

    def _natural_width(self) -> int:
        if not self._text:
            return _M.BTN
        metrics = QFontMetrics(self._font())
        width = 18 + metrics.horizontalAdvance(self._text)
        if self._glyph:
            width += _M.ICON - 1 + 5
        if self._caret:
            width += 12 + 2
        return width

    def _font(self):
        if self._glyph is None and self._text:
            return _mono_font(11.5, 500)
        return _ui_font(11.5, 500)

    def set_glyph(self, glyph: str) -> None:
        self._glyph = glyph
        self.update()

    def set_text(self, text: str) -> None:
        self._text = text
        self.setFixedWidth(self._natural_width())
        self.update()

    def set_tone(self, tone: str) -> None:
        self._tone = tone
        self.update()

    def _colours(self) -> tuple[QColor, QColor]:
        if self._tone == "on":
            return _rgba(_C.BTN_ON_BG, 0.12), QColor(_C.BTN_ON_FG)
        if self._tone == "accent":
            return _rgba(_C.ACCENT_ON_BG, 0.15), QColor(_C.ACCENT_ON_FG)
        if self._tone == "danger":
            return _rgba(_C.MUTED_BG, 0.20), QColor(_C.MUTED_FG)
        return QColor(Qt.GlobalColor.transparent), QColor(_C.BTN_IDLE_FG)

    def enterEvent(self, event) -> None:
        self._hover = True
        self.update()

    def leaveEvent(self, event) -> None:
        self._hover = False
        self.update()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if self.rect().contains(event.position().toPoint()):
            self.clicked.emit()
        event.accept()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        background, foreground = self._colours()
        if self._hover and self._tone == "idle":
            background = _rgba(_C.BTN_HOVER_BG, 0.09)
        elif self._hover:
            background = background.lighter(115)
        if background.alpha():
            path = QPainterPath()
            path.addRoundedRect(QRectF(self.rect()), _M.BTN_RADIUS, _M.BTN_RADIUS)
            painter.fillPath(path, background)

        x = 9.0 if self._text else (self.width() - _M.ICON) / 2
        if self._glyph:
            pixmap = design.icon(self._glyph, foreground).pixmap(QSize(_M.ICON, _M.ICON))
            painter.save()
            if self._rotate:
                painter.translate(x + _M.ICON / 2, self.height() / 2)
                painter.rotate(self._rotate)
                painter.translate(-_M.ICON / 2, -_M.ICON / 2)
                painter.drawPixmap(0, 0, pixmap)
            else:
                painter.drawPixmap(QPointF(x, (self.height() - _M.ICON) / 2), pixmap)
            painter.restore()
            x += _M.ICON + 5

        if self._text:
            painter.setFont(self._font())
            painter.setPen(foreground)
            width = self.width() - x - (14 if self._caret else 9)
            painter.drawText(
                QRectF(x, 0, width, self.height()),
                int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                self._text,
            )
            x += width

        if self._caret:
            caret = design.icon("chevron", QColor("#8f9689")).pixmap(QSize(12, 12))
            painter.drawPixmap(QPointF(x, (self.height() - 12) / 2), caret)
        painter.end()


class TransportBar(QWidget):
    """The 42px shell that floats over the canvas bottom.

    Same shape as the annotate bar in `review.py` so that someone who has
    used the image editor recognises this instantly -- the handoff is
    explicit that only the middle of the window should feel new.
    """

    play_toggled = pyqtSignal()
    stepped = pyqtSignal(int)
    mute_toggled = pyqtSignal()
    loop_toggled = pyqtSignal()
    speed_requested = pyqtSignal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedHeight(_M.BAR_H)

        row = QHBoxLayout(self)
        row.setContentsMargins(_M.BAR_PAD, _M.BAR_PAD, _M.BAR_PAD, _M.BAR_PAD)
        row.setSpacing(_M.BAR_GAP)

        # Play is the only pre-lit control: it is what the bar is for, and
        # a bar of uniformly idle buttons gives the eye nowhere to land.
        self.play_button = _BarButton("play", tone="on")
        self.play_button.clicked.connect(self.play_toggled)
        row.addWidget(self.play_button)

        self.back_button = _BarButton("chevron", rotate=90)
        self.back_button.clicked.connect(lambda: self.stepped.emit(-1))
        row.addWidget(self.back_button)

        self.forward_button = _BarButton("chevron", rotate=-90)
        self.forward_button.clicked.connect(lambda: self.stepped.emit(1))
        row.addWidget(self.forward_button)

        row.addWidget(self._separator())

        self.time_label = QLabel("00:00.00 / 00:00")
        self.time_label.setFont(_mono_font(12, 500))
        self.time_label.setStyleSheet(f"color: {_C.TIME_FG}; background: transparent;")
        row.addWidget(self.time_label)

        row.addWidget(self._separator())

        self.mute_button = _BarButton("speaker")
        self.mute_button.clicked.connect(self.mute_toggled)
        row.addWidget(self.mute_button)

        # Loop starts on: you are judging a short clip, and a clip that
        # stops dead after two seconds cannot be judged.
        self.loop_button = _BarButton("redo", text="Loop", tone="accent")
        self.loop_button.clicked.connect(self.loop_toggled)
        row.addWidget(self.loop_button)

        self.speed_button = _BarButton(None, text="1×", caret=True)
        self.speed_button.clicked.connect(self.speed_requested)
        row.addWidget(self.speed_button)

    @staticmethod
    def _separator() -> QWidget:
        line = QWidget()
        line.setFixedWidth(11)
        line.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        def paint(event, widget=line):
            painter = QPainter(widget)
            painter.fillRect(
                QRectF(5, (widget.height() - 20) / 2, 1, 20),
                _rgba(_C.BAR_SEP, 0.12),
            )
            painter.end()

        line.paintEvent = paint
        return line

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        body = QRectF(0.5, 0.5, self.width() - 1, self.height() - 1)
        path = QPainterPath()
        path.addRoundedRect(body, _M.BAR_RADIUS, _M.BAR_RADIUS)
        painter.fillPath(path, _rgba(_C.BAR_BG, 0.94))
        painter.setPen(_rgba(_C.BAR_BORDER, 0.10))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(path)
        painter.end()


# --------------------------------------------------------------------------
# The canvas
# --------------------------------------------------------------------------


class VideoCanvas(QWidget):
    """The workspace, the video frame on it, and the paused overlay.

    Paints the frame itself out of a `QVideoSink` rather than hosting a
    `QVideoWidget`: see the module docstring. The practical consequence is
    that everything the design floats over the picture -- the two badges,
    the transport, the pause badge -- is an ordinary sibling or a later
    brush stroke, with no native surface to punch through.

    The frame treatment is deliberately the one `review.ImageCanvas` gives
    a screenshot: fit-to-canvas, zoom on top of that, 1px border, soft
    shadow, 7px ring. A recording and a screenshot are the same kind of
    object sitting on the same kind of table.
    """

    clicked = pyqtSignal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setMouseTracking(True)
        self._frame: QImage | None = None
        self._source_size = QSize(0, 0)
        self._zoom = 100
        self._paused = True

    # -- state ----------------------------------------------------------

    def set_frame(self, image: QImage) -> None:
        self._frame = image
        if image is not None and not image.isNull():
            self._source_size = image.size()
        self.update()

    def set_source_size(self, size: QSize) -> None:
        self._source_size = size
        self.update()

    @property
    def source_size(self) -> QSize:
        return self._source_size

    def set_paused(self, paused: bool) -> None:
        self._paused = paused
        self.update()

    @property
    def zoom(self) -> int:
        return self._zoom

    def set_zoom(self, percent: int) -> None:
        low, high, _step = _M.ZOOM_STEPS
        self._zoom = max(low, min(percent, high))
        self.update()

    def current_frame(self) -> QImage | None:
        """The frame under the playhead -- what "Current frame as PNG" saves."""
        return self._frame

    # -- geometry -------------------------------------------------------

    def _scale(self) -> float:
        if self._source_size.isEmpty():
            return 1.0
        available_w = max(1, self.width() - 96)
        available_h = max(1, self.height() - 96)
        fit = min(
            available_w / self._source_size.width(),
            available_h / self._source_size.height(),
            1.0,
        )
        return fit * (self._zoom / 100)

    def video_rect(self) -> QRectF:
        if self._source_size.isEmpty():
            return QRectF()
        scale = self._scale()
        width = self._source_size.width() * scale
        height = self._source_size.height() * scale
        return QRectF(
            (self.width() - width) / 2, (self.height() - height) / 2, width, height
        )

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if self.video_rect().contains(event.position()):
            self.clicked.emit()
        event.accept()

    # -- paint ----------------------------------------------------------

    def paintEvent(self, event) -> None:
        from PyQt6.QtGui import QRadialGradient

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)

        _kind, (cx, cy), radius, stops = tokens.Gradient.WORKSPACE
        gradient = QRadialGradient(
            self.width() * cx, self.height() * cy, self.width() * radius
        )
        for position, colour in stops:
            gradient.setColorAt(position, QColor(colour))
        painter.fillRect(self.rect(), gradient)

        rect = self.video_rect()
        if rect.isEmpty():
            painter.end()
            return

        # Shadow, then ring, then picture -- the ring only reads as a halo
        # when it sits under the 1px stroke, the same order review.py uses.
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(0, 0, 0, 90))
        painter.drawRoundedRect(rect.adjusted(-2, 6, 2, 14), 10, 10)
        painter.setBrush(_rgba("#ffffff", 0.02))
        painter.drawRoundedRect(
            rect.adjusted(-_M.FRAME_RING, -_M.FRAME_RING, _M.FRAME_RING, _M.FRAME_RING),
            5,
            5,
        )

        if self._frame is not None and not self._frame.isNull():
            painter.drawImage(rect, self._frame)
        else:
            painter.fillRect(rect, QColor("#101216"))

        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QColor(tokens.Win.IMAGE_BORDER))
        painter.drawRect(rect)

        if self._paused:
            self._paint_paused(painter, rect)
        painter.end()

    def _paint_paused(self, painter: QPainter, rect: QRectF) -> None:
        painter.fillRect(rect, _rgba(_C.PAUSE_SCRIM, 0.28))
        diameter = _M.PLAY_OVERLAY_D
        badge = QRectF(0, 0, diameter, diameter)
        badge.moveCenter(rect.center())
        painter.setBrush(_rgba(_C.PAUSE_BADGE_BG, 0.82))
        painter.setPen(_rgba(_C.PAUSE_BADGE_EDGE, 0.16))
        painter.drawEllipse(badge)
        glyph = design.icon("play", QColor(_C.PAUSE_BADGE_FG)).pixmap(QSize(30, 30))
        painter.drawPixmap(
            QPointF(badge.center().x() - 13, badge.center().y() - 15), glyph
        )


# --------------------------------------------------------------------------
# Export
# --------------------------------------------------------------------------

# snipux does not depend on ffmpeg and never requires it. It *uses* one if
# the system already has it, because the difference is large and visible:
# with it, "Export MP4" is real H.264 that plays in a browser, and GIF and
# trimmed WebM become possible at all. Without it everything still works,
# one codec down. Detected at runtime, verified by asking the binary what
# it can encode, and cached -- never assumed from its presence on PATH,
# since a minimal build may carry none of the encoders that matter.
_FFMPEG_NEEDS = ("libx264", "libvpx-vp9", "gif")
_ffmpeg_cache: "str | None | object" = None
_UNPROBED = object()
_ffmpeg_cache = _UNPROBED


def system_ffmpeg() -> str | None:
    """Path to a system ffmpeg that can encode what the menu offers, or None.

    Probed once per process. `-encoders` is a ~100ms subprocess and the
    answer cannot change while snipux runs, so paying for it lazily on the
    first export or menu open costs nothing at startup.
    """
    global _ffmpeg_cache
    if _ffmpeg_cache is not _UNPROBED:
        return _ffmpeg_cache

    _ffmpeg_cache = None
    binary = shutil.which("ffmpeg")
    if binary is None:
        return None
    try:
        listing = subprocess.run(
            [binary, "-hide_banner", "-encoders"],
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        # A binary that will not answer is a binary we will not rely on.
        return None
    if all(f" {name} " in listing for name in _FFMPEG_NEEDS):
        _ffmpeg_cache = binary
    return _ffmpeg_cache


def reset_ffmpeg_probe() -> None:
    """Forget the cached probe. For tests, which need both worlds."""
    global _ffmpeg_cache
    _ffmpeg_cache = _UNPROBED


# What Qt ALONE can write, measured rather than assumed:
# QMediaFormat *reports* H264/H265/MPEG4 encoders for the MPEG4 container,
# but the bundled FFmpeg (7.1.5, LGPL) carries only the hardware H.264
# encoders -- `h264_nvenc` and `h264_vaapi`, no software x264 -- and none at
# all for VP8/VP9, while QImageWriter has no GIF plugin. So a trimmed WebM
# and a GIF cannot be produced without a dependency snipux does not have,
# and the honest thing is to leave both rows in the menu, disabled, saying
# why. A missing row reads as a bug; a greyed row with a reason reads as a
# limit. See snipux/__init__.py for why H.264 itself is not assumed either.
EXPORT_UNAVAILABLE = {
    "gif": "No GIF encoder in this build",
    "webm": "Needs a VP9 encoder to re-encode",   # only when trimmed; see below
}


def export_availability(trimmed: bool, *, ffmpeg: bool | None = None) -> dict[str, str]:
    """Format id -> why it is unavailable. Absent means available.

    With a system ffmpeg every format the design offers is reachable, so
    the answer is "nothing is unavailable" and the menu is the one the
    handoff drew.

    Without one, WebM is the interesting case: untrimmed it is a straight
    copy of what was recorded, which always works and needs no encoder at
    all, and trimmed it would need one Qt does not carry. So its
    availability depends on the trim, which is exactly what the handoff's
    own note ("no re-encode when untrimmed") is describing.

    `ffmpeg=None` asks the system; the tests pass it explicitly so both
    worlds are covered on any machine.
    """
    if ffmpeg is None:
        ffmpeg = system_ffmpeg() is not None
    if ffmpeg:
        return {}
    unavailable = {"gif": EXPORT_UNAVAILABLE["gif"]}
    if trimmed:
        unavailable["webm"] = EXPORT_UNAVAILABLE["webm"]
    return unavailable


class FfmpegExporter(QObject):
    """Writes the trimmed range with the system ffmpeg. Never touches the source.

    Preferred over `Exporter` whenever `system_ffmpeg()` finds one, because
    it is the difference between an MP4 that plays in a browser and one
    that does not, and because GIF and a re-encoded WebM are not reachable
    any other way. `Exporter` remains the fallback and is not going away:
    ffmpeg is an optional convenience, never a requirement.

    Runs as a `QProcess` rather than a blocking `subprocess`, so the window
    keeps painting and the footer can report progress -- ffmpeg's own
    `-progress pipe:1` reports `out_time_us` against the range being
    written, which is exactly the fraction wanted.
    """

    progressed = pyqtSignal(float)
    finished = pyqtSignal(str)
    failed = pyqtSignal(str)

    def __init__(
        self,
        source: Path,
        destination: Path,
        state: TrimState,
        format_id: str,
        *,
        muted: bool,
        binary: str,
        fps: float = tokens.PLAYER_FPS,
        parent: QObject | None = None,
    ):
        super().__init__(parent)
        self._source = source
        self._destination = destination
        self._start = state.start
        self._duration = max(0.05, state.kept)
        self._format = format_id
        self._muted = muted
        self._binary = binary
        # A GNOME screencast declares `r_frame_rate=1000/1` -- a millisecond
        # timebase, not a thousand frames a second -- with no average rate
        # at all. Copied through, that made ffmpeg duplicate ~31 real frames
        # into 1,455 and stamp the output as 1000fps: a file every player
        # has to work around. Anything outside a plausible range is not a
        # frame rate, it is a timebase, and gets replaced.
        self._fps = fps if 1 <= fps <= 120 else float(tokens.PLAYER_FPS)
        self._process = None
        self._tail: list[str] = []
        self._diagnosis = ""

    def _arguments(self) -> list[str]:
        # `-ss` before `-i` seeks by keyframe, which is fast; the re-encode
        # that follows is what makes the cut land on the exact frame, so
        # there is no accuracy to trade away here.
        args = ["-hide_banner", "-nostdin", "-y",
                "-ss", f"{self._start:.3f}", "-i", str(self._source),
                "-t", f"{self._duration:.3f}"]

        if self._format == "mp4":
            args += [
                # H.264 in yuv420p cannot encode an odd width or height, and
                # a snip is whatever rectangle was dragged -- the first real
                # recording tried here was 983x680 and libx264 refused it
                # outright. Rounding DOWN to even loses at most one row or
                # column of pixels; rounding up would invent one.
                "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                # yuv420p rather than whatever the source was: it is the
                # only chroma layout every browser and phone decodes.
                "-pix_fmt", "yuv420p", "-movflags", "+faststart",
            ]
            args += ["-an"] if self._muted else ["-c:a", "aac", "-b:a", "128k"]
        elif self._format == "webm":
            args += ["-c:v", "libvpx-vp9", "-crf", "32", "-b:v", "0",
                     # VP9 is slow by default; `good`/4 is the usual
                     # sane-quality-in-finite-time pairing for a short clip.
                     "-row-mt", "1", "-deadline", "good", "-cpu-used", "4"]
            args += ["-an"] if self._muted else ["-c:a", "libopus", "-b:a", "128k"]
        elif self._format == "gif":
            # Two passes in one command: a palette generated from this clip
            # beats the fixed 216-colour web palette by a wide margin, and a
            # screen recording is mostly flat colour where the difference is
            # most visible. 15fps and 640px wide because the menu's own note
            # says GIF is big above ten seconds and this is where that is
            # kept honest.
            args += ["-vf", ("fps=15,scale=640:-1:flags=lanczos,split[a][b];"
                             "[a]palettegen=stats_mode=diff[p];"
                             "[b][p]paletteuse=dither=bayer:bayer_scale=5"),
                     "-loop", "0", "-an"]
        else:
            raise ValueError(f"ffmpeg does not handle {self._format!r} here")

        if self._format != "gif":
            # GIF sets its own rate inside the filter graph.
            args += ["-fps_mode", "cfr", "-r", f"{self._fps:g}"]
        return args + ["-progress", "pipe:1", "-nostats", str(self._destination)]

    def start(self) -> None:
        from PyQt6.QtCore import QProcess

        self._process = QProcess(self)
        self._process.setProcessChannelMode(QProcess.ProcessChannelMode.SeparateChannels)
        self._process.readyReadStandardOutput.connect(self._read_progress)
        self._process.readyReadStandardError.connect(self._read_errors)
        self._process.finished.connect(self._on_finished)
        self._process.errorOccurred.connect(
            lambda *_: self.failed.emit("ffmpeg could not be started")
        )
        self._process.start(self._binary, self._arguments())

    def _read_progress(self) -> None:
        text = bytes(self._process.readAllStandardOutput()).decode("utf-8", "replace")
        for line in text.splitlines():
            key, _, value = line.partition("=")
            if key.strip() != "out_time_us":
                continue
            try:
                written = int(value) / 1_000_000
            except ValueError:
                continue
            self.progressed.emit(max(0.0, min(1.0, written / self._duration)))

    def _read_errors(self) -> None:
        text = bytes(self._process.readAllStandardError()).decode("utf-8", "replace")
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            # The diagnosis and the epitaph are different lines, and ffmpeg
            # prints the diagnosis first: "width not divisible by 2" comes
            # several lines before "Conversion failed!". Keeping only the
            # tail reported the epitaph, which tells the user nothing.
            if any(word in line.lower() for word in ("not divisible", "invalid",
                                                     "no such", "unable",
                                                     "could not", "unsupported")):
                self._diagnosis = self._diagnosis or line
            self._tail = (self._tail + [line])[-6:]

    def _on_finished(self, code: int, _status) -> None:
        if code == 0 and self._destination.exists() and self._destination.stat().st_size:
            self.progressed.emit(1.0)
            self.finished.emit(str(self._destination))
            return
        reason = self._diagnosis or next(
            (line for line in reversed(self._tail) if line.strip()),
            f"ffmpeg exited with {code}",
        )
        self.failed.emit(reason.strip()[:160])


class Exporter(QObject):
    """Writes the trimmed range out as a new file. Never touches the source.

    Video goes source -> `QMediaPlayer` -> `QVideoSink` -> `QVideoFrameInput`
    -> `QMediaRecorder`, with every frame's timestamp rebased so the output
    starts at zero rather than at the in-point. Playing the range in real
    time is the cost of this pipeline: a 20-second export takes about
    twenty seconds, which is why the window reports progress rather than
    freezing.

    Muting means dropping the audio track, not silencing a monitor, so a
    muted export simply never wires up an audio input.
    """

    progressed = pyqtSignal(float)          # 0..1
    finished = pyqtSignal(str)              # written path
    failed = pyqtSignal(str)

    # Private, and the whole reason this class has a signal it does not
    # expose: `QVideoSink.videoFrameChanged` is emitted on the decoder's own
    # QThread, and QMediaRecorder refuses everything -- record(), its
    # settings, sendVideoFrame() -- when it is touched from anywhere but the
    # thread it lives on, reporting "Operation not permitted" and stopping
    # after a single frame. Emitting a signal from the decoder thread to a
    # slot on this object queues the work onto the main thread, which is the
    # supported way to cross that boundary.
    _frames_pending = pyqtSignal()

    def __init__(
        self,
        source: Path,
        destination: Path,
        state: TrimState,
        *,
        muted: bool,
        parent: QObject | None = None,
    ):
        super().__init__(parent)
        self._source = source
        self._destination = destination
        self._start = state.start
        self._end = state.end
        self._muted = muted
        self._done = False
        self._fps = tokens.PLAYER_FPS
        self._format_cache = None
        self._pending: list = []
        self._queue: list = []
        self._ended = False
        self._frames_pending.connect(self._pump)

    def start(self) -> None:
        from PyQt6.QtMultimedia import (
            QAudioBufferInput,
            QMediaCaptureSession,
            QMediaFormat,
            QMediaRecorder,
            QVideoFrameInput,
        )

        self._session = QMediaCaptureSession(self)
        self._video_input = QVideoFrameInput(self)
        self._session.setVideoFrameInput(self._video_input)

        self._audio_input = None
        if not self._muted:
            self._audio_input = QAudioBufferInput(self)
            self._session.setAudioBufferInput(self._audio_input)

        self._recorder = QMediaRecorder(self)
        self._recorder.setVideoFrameRate(self._fps)
        media_format = QMediaFormat(QMediaFormat.FileFormat.MPEG4)
        media_format.setVideoCodec(QMediaFormat.VideoCodec.H264)
        media_format.setAudioCodec(QMediaFormat.AudioCodec.AAC)
        self._recorder.setMediaFormat(media_format)
        self._recorder.setOutputLocation(QUrl.fromLocalFile(str(self._destination)))
        self._session.setRecorder(self._recorder)
        # `errorOccurred` and `recorderStateChanged` cannot be connected from
        # PyQt6 at all -- this build has no converter registered for
        # QMediaRecorder's own Error and RecorderState enums, and the
        # connect() is refused rather than failing later. `errorChanged`
        # carries no arguments and `errorString()` is an ordinary QString,
        # so the same fact is reachable without the enum.
        self._recorder.errorChanged.connect(self._on_recorder_error)
        self._recorder.actualLocationChanged.connect(self._on_location)
        self._written: Path = self._destination

        self._sink = QVideoSink(self)
        self._player = QMediaPlayer(self)
        self._player.setVideoSink(self._sink)
        # No QAudioOutput: exporting must not play the clip out loud.
        self._sink.videoFrameChanged.connect(self._on_frame)
        self._player.mediaStatusChanged.connect(self._on_status)
        self._player.errorOccurred.connect(
            lambda *args: self._fail(
                self._player.errorString() or "the source could not be read"
            )
        )
        self._video_input.readyToSendVideoFrame.connect(self._drain)

        # `record()` deliberately not called yet: a QMediaRecorder refuses
        # every setting once it is rolling ("Operation not permitted"), and
        # both the frame rate and the resolution are only known after the
        # source has loaded and delivered a frame. Recording starts in
        # _frame_format(), which is the first moment all of it is known.
        self._player.setSource(QUrl.fromLocalFile(str(self._source)))

    def _adopt_source_rate(self) -> None:
        """Encode at the rate the source was recorded at, when it says.

        A 60fps screen recording re-encoded at a declared 30 plays back at
        half speed in players that trust the container's rate over the
        frame timestamps.
        """
        from PyQt6.QtMultimedia import QMediaMetaData

        rate = self._player.metaData().value(QMediaMetaData.Key.VideoFrameRate)
        if isinstance(rate, (int, float)) and rate > 0:
            self._fps = float(rate)
            self._recorder.setVideoFrameRate(self._fps)

    def _on_recorder_error(self) -> None:
        reason = self._recorder.errorString()
        if reason:
            self._fail(reason)

    def _on_location(self, url) -> None:
        """Where the recorder actually wrote.

        Taken from the recorder rather than assumed: it is free to correct
        the extension to match the container it chose, and reporting the
        path we asked for would name a file that may not exist.
        """
        local = url.toLocalFile()
        if local:
            self._written = Path(local)

    # -- video ----------------------------------------------------------

    def _on_status(self, status) -> None:
        if self._done:
            return
        if status == QMediaPlayer.MediaStatus.LoadedMedia:
            self._adopt_source_rate()
            self._player.setPosition(int(self._start * 1000))
            self._player.play()
        elif status == QMediaPlayer.MediaStatus.EndOfMedia:
            self._ended = True
            self._pump()

    def _frame_format(self, size):
        """One `QVideoFrameFormat` for the whole export, made once.

        Every frame must carry a stream frame rate. `QVideoFrame(QImage)`
        is the obvious way to build a frame and leaves that rate at zero,
        which the encoder rejects -- it logs "Invalid frameRate 0" and then
        fails the recording after a single frame.

        This is also where `record()` finally happens: a QMediaRecorder
        refuses every setting once it is rolling, and the resolution is not
        known until a frame has arrived, so starting any earlier means
        recording at the wrong size or not at all.
        """
        from PyQt6.QtMultimedia import QVideoFrameFormat

        if self._format_cache is None:
            fmt = QVideoFrameFormat(
                size, QVideoFrameFormat.PixelFormat.Format_BGRX8888
            )
            fmt.setStreamFrameRate(float(self._fps))
            self._format_cache = fmt
            self._recorder.setVideoResolution(size)
            self._recorder.record()
        return self._format_cache

    def _on_frame(self, frame) -> None:
        """Decoder thread. Copies pixels out and hands them over; nothing else.

        Everything that talks to the recorder happens in `_pump` instead --
        see `_frames_pending`.
        """
        if self._done or self._ended or not frame.isValid():
            return
        # The frame's own timestamp, not the player's position: the player
        # is a few frames ahead of whatever is being handed over here, and
        # a trim cut against the wrong one lands early.
        position = frame.startTime() / 1_000_000
        if position < self._start - 0.05:
            return
        if position > self._end:
            self._ended = True
            self._frames_pending.emit()
            return

        image = frame.toImage().convertToFormat(QImage.Format.Format_RGB32)
        if image.isNull():
            return
        # Copied out because the sink owns the buffer behind a delivered
        # frame and reuses it the moment this handler returns -- holding one
        # for later reads memory the decoder has moved on from, which
        # crashed the process outright rather than merely corrupting a frame.
        self._pending.append((position, image.copy()))
        self._frames_pending.emit()

    def _pump(self) -> None:
        """Main thread. Builds real video frames and feeds the recorder."""
        from PyQt6.QtMultimedia import QVideoFrame

        while self._pending and not self._done:
            position, image = self._pending.pop(0)
            frame = QVideoFrame(self._frame_format(image.size()))
            if not frame.map(QVideoFrame.MapMode.WriteOnly):
                continue
            stride = frame.bytesPerLine(0)
            row_bytes = image.width() * 4
            bits = frame.bits(0)
            bits.setsize(frame.mappedBytes(0))
            for y in range(image.height()):
                start = y * stride
                bits[start:start + row_bytes] = image.constScanLine(y).asstring(row_bytes)
            frame.unmap()

            offset_us = int(max(0.0, position - self._start) * 1_000_000)
            frame.setStartTime(offset_us)
            frame.setEndTime(offset_us + int(1_000_000 / self._fps))
            self._queue.append(frame)

            span = max(0.001, self._end - self._start)
            self.progressed.emit(min(1.0, (position - self._start) / span))

        self._drain()
        if self._ended and not self._pending:
            self._stop()

    def _drain(self) -> None:
        while self._queue:
            if not self._video_input.sendVideoFrame(self._queue[0]):
                return          # back-pressure; readyToSendVideoFrame retries
            self._queue.pop(0)

    # -- finishing ------------------------------------------------------

    def _stop(self) -> None:
        if self._done:
            return
        self._done = True
        self._player.stop()
        self._drain()
        self._recorder.stop()
        # The recorder finalises the container asynchronously; reporting the
        # path before that lands would name a file that is not yet playable.
        QTimer.singleShot(400, self._announce)

    def _announce(self) -> None:
        if self._written.exists() and self._written.stat().st_size > 0:
            self.finished.emit(str(self._written))
        else:
            self.failed.emit("the export produced no file")

    def _fail(self, reason: str) -> None:
        if self._done:
            return
        self._done = True
        self.failed.emit(reason)



def export_copy(source: Path, destination: Path) -> str:
    """The untrimmed WebM path: what was recorded, byte for byte.

    `copy2` rather than a re-encode because there is nothing to change --
    re-encoding an untrimmed recording would cost time and a generation of
    quality to produce the same clip.
    """
    shutil.copy2(source, destination)
    return str(destination)


def export_frame(image: QImage, destination: Path) -> str:
    """"Current frame as PNG" -- how a still gets out without leaving the window."""
    if image is None or image.isNull():
        raise ValueError("no frame under the playhead")
    if not image.save(str(destination), "PNG"):
        raise OSError(f"could not write {destination}")
    return str(destination)


# --------------------------------------------------------------------------
# The window
# --------------------------------------------------------------------------


class _SplitExportButton(QWidget):
    """`Export MP4` with a caret for the other formats.

    Split rather than a plain button because the footer's primary action
    has to be one click for the common case and still reachable for the
    other three. Export is the primary here, unlike the image editor where
    Copy is: trimming re-encodes, so a file must be written, and a
    clipboard-only result would be a lie about what happened.
    """

    triggered = pyqtSignal()
    menu_requested = pyqtSignal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._face = "Export MP4"
        self._hover = ""
        self.setFixedHeight(_M.ACTION_BTN_H)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMouseTracking(True)
        self._resize()

    def set_face(self, face: str) -> None:
        self._face = face
        self._resize()
        self.update()

    def _resize(self) -> None:
        metrics = QFontMetrics(_ui_font(12.5, 600))
        self.setFixedWidth(
            30 + 15 + 7 + metrics.horizontalAdvance(self._face) + _M.SPLIT_CARET_W
        )

    def _caret_rect(self) -> QRectF:
        return QRectF(self.width() - _M.SPLIT_CARET_W, 0, _M.SPLIT_CARET_W, self.height())

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        where = "caret" if self._caret_rect().contains(event.position()) else "face"
        if where != self._hover:
            self._hover = where
            self.update()

    def leaveEvent(self, event) -> None:
        self._hover = ""
        self.update()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if self._caret_rect().contains(event.position()):
            self.menu_requested.emit()
        else:
            self.triggered.emit()
        event.accept()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        radius = _M.ACTION_RADIUS
        accent = QColor(tokens.Color.ACCENT)
        path = QPainterPath()
        path.addRoundedRect(QRectF(self.rect()), radius, radius)
        painter.fillPath(path, accent.lighter(106) if self._hover else accent)

        # The seam: the two halves are one colour, so without it the caret
        # is not visibly a separate target.
        seam = self.width() - _M.SPLIT_CARET_W
        painter.fillRect(QRectF(seam, 4, 1, self.height() - 8), _rgba("#15170e", 0.22))

        ink = QColor(tokens.Color.ACCENT_FG)
        glyph = design.icon("save", ink).pixmap(QSize(15, 15))
        painter.drawPixmap(QPointF(15, (self.height() - 15) / 2), glyph)
        painter.setFont(_ui_font(12.5, 600))
        painter.setPen(ink)
        painter.drawText(
            QRectF(15 + 15 + 7, 0, seam - 37, self.height()),
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            self._face,
        )
        caret = design.icon("chevron", ink).pixmap(QSize(13, 13))
        centre = self._caret_rect().center()
        painter.drawPixmap(QPointF(centre.x() - 6.5, centre.y() - 6.5), caret)
        painter.end()


class PlayerWindow(WinWindow):
    """The window a recording opens into.

    Owns the one `QMediaPlayer` the user actually watches (the probes and
    the exporter each have their own, deliberately -- sharing one would
    make a filmstrip decode fight a playback), and holds `TrimState`, which
    every readout in the window is a different phrasing of.
    """

    exported = pyqtSignal(str)

    def __init__(
        self,
        path: str | Path,
        parent: QWidget | None = None,
        *,
        autoplay: bool = False,
    ):
        self.path = Path(path)
        super().__init__(self.path.name, size=_M.WINDOW_MIN, parent=parent)
        self.setMinimumSize(*_M.WINDOW_MIN)

        self.state = TrimState()
        self._muted = False
        self._loop = True
        self._speed = "1"
        self._format = tokens.EXPORT_DEFAULT
        self._saved = True
        self._menu: FlowMenu | None = None
        self._exporter = None
        self._source_fps: float = float(tokens.PLAYER_FPS)

        self._build_title_extras()
        self._build_body()
        self._build_footer_row()
        self._build_media(autoplay)
        self._sync_all()

    # -- construction ---------------------------------------------------

    def _build_title_extras(self) -> None:
        """The dot-and-label pending indicator, after the mono detail line.

        Its whole job is to answer "is what I am looking at what is on
        disk" without the user having to look at the footer, so it lives in
        the title bar where a document's dirty mark belongs.
        """
        row = self.title_bar.layout()
        self.pending_label = QLabel()
        self.pending_label.setFont(_ui_font(11, 400))
        row.insertWidget(row.count() - 4, self.pending_label)

    def _build_body(self) -> None:
        column = QVBoxLayout(self.body)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(0)

        self.canvas = VideoCanvas()
        self.canvas.clicked.connect(self.toggle_play)
        column.addWidget(self.canvas, 1)

        # Floating children of the canvas rather than rows in the column:
        # the design has all three *over* the picture, and a layout row
        # would push the picture up instead.
        from .review import _Badge, _ZoomBadge

        self.size_badge = _Badge(self.canvas)
        self.zoom_badge = _ZoomBadge(self.canvas)
        self.zoom_badge.zoomChanged.connect(self._on_zoom)

        self.transport = TransportBar(self.canvas)
        self.transport.play_toggled.connect(self.toggle_play)
        self.transport.stepped.connect(self.step_frame)
        self.transport.mute_toggled.connect(self.toggle_mute)
        self.transport.loop_toggled.connect(self.toggle_loop)
        self.transport.speed_requested.connect(self._open_speed_menu)

        column.addWidget(self._build_timeline_panel())

    def _build_timeline_panel(self) -> QWidget:
        panel = QWidget()
        panel.setStyleSheet(
            f"background: {tokens.Win.CHROME_BG};"
            f" border-top: 1px solid {tokens.Win.SEPARATOR};"
        )
        pad_top, pad_h, pad_bottom = _M.PANEL_PAD
        column = QVBoxLayout(panel)
        column.setContentsMargins(pad_h, pad_top, pad_h, pad_bottom)
        column.setSpacing(_M.PANEL_GAP)

        row = QHBoxLayout()
        row.setSpacing(10)
        heading = QLabel("TRIM")
        heading.setFont(_ui_font(10.5, 600))
        heading.setStyleSheet(
            f"color: {tokens.Win.TEXT_SECTION}; letter-spacing: 1px; border: none;"
        )
        row.addWidget(heading)

        self.mark_in_button = _RowButton("Start here", "I", edge="left")
        self.mark_in_button.clicked.connect(self.mark_in)
        row.addWidget(self.mark_in_button)

        self.mark_out_button = _RowButton("End here", "O", edge="right")
        self.mark_out_button.clicked.connect(self.mark_out)
        row.addWidget(self.mark_out_button)

        self.reset_button = _RowButton("Reset", "", flat=True)
        self.reset_button.clicked.connect(self.reset_trim)
        row.addWidget(self.reset_button)

        row.addStretch()
        self.readout = _TrimReadout()
        row.addWidget(self.readout)
        column.addLayout(row)

        self.rail = TimelineRail(self.state)
        self.rail.trim_changed.connect(self._on_trim_changed)
        self.rail.scrubbed.connect(self._on_scrubbed)
        column.addWidget(self.rail)
        return panel

    def _build_footer_row(self) -> None:
        from .winchrome import SecondaryButton

        status = QWidget()
        status.setStyleSheet("background: transparent;")
        stack = QVBoxLayout(status)
        stack.setContentsMargins(0, 0, 0, 0)
        stack.setSpacing(5)

        state_row = QHBoxLayout()
        state_row.setContentsMargins(0, 0, 0, 0)
        state_row.setSpacing(7)
        self.status_icon = QLabel()
        self.status_icon.setFixedSize(14, 14)
        state_row.addWidget(self.status_icon)
        self.status_label = QLabel()
        self.status_label.setFont(_ui_font(12, 500))
        state_row.addWidget(self.status_label)
        state_row.addStretch()
        stack.addLayout(state_row)

        self.path_label = QLabel(self._display_path())
        self.path_label.setFont(_mono_font(11.5))
        self.path_label.setStyleSheet(f"color: {tokens.Win.TEXT_MUTED};")
        stack.addWidget(self.path_label)
        self.footer_left.addWidget(status)

        self.copy_button = SecondaryButton("Copy file")
        self.copy_button.setToolTip(
            "Copies a file reference -- pastes into a chat or folder, "
            "not an image editor"
        )
        self.copy_button.clicked.connect(self.copy_file)
        self.footer_right.addWidget(self.copy_button)

        self.folder_button = SecondaryButton("Show in Folder")
        self.folder_button.clicked.connect(self.show_in_folder)
        self.footer_right.addWidget(self.folder_button)

        self.export_button = _SplitExportButton()
        self.export_button.triggered.connect(self.run_export)
        self.export_button.menu_requested.connect(self._open_export_menu)
        self.footer_right.addWidget(self.export_button)

    def _build_media(self, autoplay: bool) -> None:
        self._sink = QVideoSink(self)
        self._audio_out = QAudioOutput(self)
        self.player = QMediaPlayer(self)
        self.player.setVideoSink(self._sink)
        self.player.setAudioOutput(self._audio_out)
        self._sink.videoFrameChanged.connect(self._on_video_frame)
        self.player.durationChanged.connect(self._on_duration)
        self.player.positionChanged.connect(self._on_position)
        self.player.playbackStateChanged.connect(lambda *_: self._sync_transport())
        self.player.metaDataChanged.connect(self._on_metadata)
        self.player.setSource(QUrl.fromLocalFile(str(self.path)))
        self._autoplay = autoplay
        self._probes_started = False

    def _on_metadata(self) -> None:
        """Adopt the resolution as soon as the container reports it.

        Waiting for the first decoded frame would leave the title bar and
        the canvas badge reading "-" until something was played, and the
        window opens paused.
        """
        from PyQt6.QtMultimedia import QMediaMetaData

        size = self.player.metaData().value(QMediaMetaData.Key.Resolution)
        if isinstance(size, QSize) and not size.isEmpty():
            self.canvas.set_source_size(size)
            self._sync_chrome()
            self._sync_readout()

        rate = self.player.metaData().value(QMediaMetaData.Key.VideoFrameRate)
        if isinstance(rate, (int, float)) and 1 <= rate <= 120:
            self._source_fps = float(rate)

    # -- media callbacks -------------------------------------------------

    def _on_duration(self, milliseconds: int) -> None:
        if milliseconds <= 0:
            return
        duration = milliseconds / 1000
        first = self.state.duration <= 0
        self.state.duration = duration
        if first:
            # A freshly opened recording is untrimmed. Only set the range
            # here, not on every duration signal -- the backend re-reports
            # the duration during playback and that would silently undo a
            # trim the user had already made.
            self.state.start = 0.0
            self.state.end = duration
            self._start_probes()
            if self._autoplay:
                self.player.play()
            else:
                # A poster frame: the window opens paused, and paused over
                # nothing is a black rectangle with a play badge on it.
                # Pausing before seeking is what makes the backend deliver
                # a frame at all -- a stopped player answers a seek with
                # silence.
                self.player.pause()
                self.player.setPosition(0)
        self._sync_all()

    def _start_probes(self) -> None:
        if self._probes_started:
            return
        self._probes_started = True
        self._filmstrip = FilmstripProbe(
            self.path, self.state.duration, _M.FILMSTRIP_CELLS, self
        )
        self._filmstrip.cell_ready.connect(self.rail.set_thumbnail)
        if self.player.audioTracks():
            self._waveform = WaveformProbe(
                self.path, self.state.duration, _M.WAVE_BARS, self
            )
            self._waveform.ready.connect(self._on_peaks)

    def _on_peaks(self, peaks: list) -> None:
        self.rail.set_peaks(peaks)
        self.rail.sync()

    def _on_video_frame(self, frame) -> None:
        if not frame.isValid():
            return
        image = frame.toImage()
        if not image.isNull():
            self.canvas.set_frame(image)

    def _on_position(self, milliseconds: int) -> None:
        seconds = milliseconds / 1000
        # Clamped here rather than trusted to a seek: the handoff's note,
        # and true in practice -- a seek lands on the nearest frame, not on
        # the exact second asked for, so the playhead has to be corrected
        # after the fact or it drifts outside the range being kept.
        if seconds >= self.state.end - 0.01:
            if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
                if self._loop:
                    self.player.setPosition(int(self.state.start * 1000))
                    return
                self.player.pause()
            seconds = min(seconds, self.state.end)
        elif seconds < self.state.start:
            seconds = self.state.start
        self.state.position = seconds
        self.rail.sync()
        self._sync_transport()

    # -- commands --------------------------------------------------------

    def toggle_play(self) -> None:
        self._close_menu()
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
        else:
            if self.state.position >= self.state.end - 0.05:
                self.player.setPosition(int(self.state.start * 1000))
            self.player.play()
        self._sync_transport()

    def step_frame(self, direction: int) -> None:
        self.player.pause()
        target = self.state.position + direction / tokens.PLAYER_FPS
        target = max(self.state.start, min(target, self.state.end))
        self.player.setPosition(int(target * 1000))
        self.state.position = target
        self.rail.sync()
        self._sync_transport()

    def toggle_mute(self) -> None:
        self._muted = not self._muted
        # The monitor follows the export decision so what you hear matches
        # what you would get -- but the *decision* is about the track, and
        # that is what the exporter reads.
        self._audio_out.setMuted(self._muted)
        self.rail.set_muted(self._muted)
        self._mark_dirty()
        self._sync_transport()

    def toggle_loop(self) -> None:
        self._loop = not self._loop
        self._sync_transport()

    def set_speed(self, speed: str) -> None:
        self._speed = speed
        self.player.setPlaybackRate(float(speed))
        self._sync_transport()

    def mark_in(self) -> None:
        self.state.start = min(
            self.state.position, self.state.end - _M.MIN_RANGE_S
        )
        self.state.start = max(0.0, self.state.start)
        self._on_trim_changed()

    def mark_out(self) -> None:
        self.state.end = max(
            self.state.position, self.state.start + _M.MIN_RANGE_S
        )
        self.state.end = min(self.state.duration, self.state.end)
        self._on_trim_changed()

    def reset_trim(self) -> None:
        self.state.start = 0.0
        self.state.end = self.state.duration
        self._on_trim_changed()

    def _on_trim_changed(self) -> None:
        self._mark_dirty()
        self.rail.sync()
        self._sync_all()

    def _on_scrubbed(self, seconds: float) -> None:
        self.player.pause()
        self.player.setPosition(int(seconds * 1000))
        self._sync_transport()

    def _on_zoom(self, percent: int) -> None:
        self.canvas.set_zoom(percent)

    def _mark_dirty(self) -> None:
        self._saved = False
        self._sync_chrome()

    # -- keeping every readout saying the same thing ---------------------

    def _sync_all(self) -> None:
        self._sync_chrome()
        self._sync_transport()
        self._sync_readout()
        self.rail.sync()
        self._place_floating()

    def _sync_chrome(self) -> None:
        size = self.canvas.source_size
        detail = []
        if not size.isEmpty():
            detail.append(f"{size.width()} × {size.height()}")
        detail.append(f"{tokens.PLAYER_FPS} fps")
        detail.append(format_clock(self.state.duration))
        self.title_detail.setText(" · ".join(detail))

        pending = "No pending edits" if self._saved else "Unsaved trim"
        colour = tokens.Win.TEXT_FAINT if self._saved else _C.DIRTY_FG
        self.pending_label.setText(f"●  {pending}")
        self.pending_label.setStyleSheet(f"color: {colour};")

        self._set_status(
            "Saved" if self._saved else "Edited — not exported", ok=self._saved
        )

        for fid, _glyph, label, _note, _rate in tokens.EXPORT_FORMATS:
            if fid == self._format:
                self.export_button.set_face(f"Export {label.split(' ')[0]}")
                break

    def _sync_transport(self) -> None:
        playing = (
            self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState
        )
        self.transport.play_button.set_glyph("pause" if playing else "play")
        self.canvas.set_paused(not playing)

        # Position *within the trim range* over the trimmed duration: what
        # you see is what you will get, so the numbers describe the clip
        # being made rather than the file it came from.
        within = max(0.0, self.state.position - self.state.start)
        self.transport.time_label.setText(
            f"{format_timecode(within)} / {format_clock(self.state.kept)}"
        )
        self.transport.mute_button.set_glyph("mute" if self._muted else "speaker")
        self.transport.mute_button.set_tone("danger" if self._muted else "idle")
        self.transport.mute_button.setToolTip(
            "Unmute — the export keeps audio (M)"
            if self._muted
            else "Mute — the export drops the audio track (M)"
        )
        self.transport.loop_button.set_tone("accent" if self._loop else "idle")
        self.transport.speed_button.set_text(f"{self._speed}×")
        self.transport.speed_button.set_tone(
            "idle" if self._speed == "1" else "accent"
        )
        self.transport.adjustSize()
        self._place_floating()

    def _sync_readout(self) -> None:
        self.readout.set_state(self.state)
        self.reset_button.set_muted_look(not self.state.trimmed)
        trim = (
            f"trimmed to {format_clock(self.state.kept)}"
            if self.state.trimmed
            else "full length"
        )
        size = self.canvas.source_size
        dimensions = (
            f"{size.width()} × {size.height()}" if not size.isEmpty() else "—"
        )
        self.size_badge.setText(f"{dimensions}  ·  {trim}")
        self.size_badge.setStyleSheet(
            self.size_badge.styleSheet().split("color:")[0]
            + f"color: {_C.KEPT_FG if self.state.trimmed else _C.BADGE_FG};"
        )
        self.size_badge.adjustSize()

    def _place_floating(self) -> None:
        """The three things that sit over the canvas, positioned by hand.

        Absolute placement rather than a layout because all three are
        *over* the picture -- a layout would reserve space and shrink it.
        """
        inset_x, inset_y = _M.BADGE_INSET
        self.size_badge.move(inset_x, inset_y)
        self.zoom_badge.adjustSize()
        self.zoom_badge.move(
            self.canvas.width() - self.zoom_badge.width() - inset_x, inset_y
        )
        self.transport.move(
            round((self.canvas.width() - self.transport.width()) / 2),
            self.canvas.height() - self.transport.height() - _M.BAR_BOTTOM,
        )
        for widget in (self.size_badge, self.zoom_badge, self.transport):
            widget.raise_()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._place_floating()
        self._elide_path()

    def _elide_path(self) -> None:
        """Fit the path into whatever the footer left it.

        Elided at the *front*: the end of a path is the filename, which is
        what identifies the recording, and a middle elision would cut the
        one part worth reading.
        """
        full = self._display_path()
        metrics = QFontMetrics(self.path_label.font())
        available = max(80, self.path_label.width())
        self.path_label.setText(
            metrics.elidedText(full, Qt.TextElideMode.ElideLeft, available)
        )
        self.path_label.setToolTip(full)

    # -- menus ------------------------------------------------------------

    def _close_menu(self) -> None:
        if self._menu is not None:
            self._menu.close()
            self._menu = None

    def _open_speed_menu(self) -> None:
        self._close_menu()
        rows = [(value, f"{value}×", "", "", "") for value in tokens.SPEEDS]
        self._menu = FlowMenu(rows, self._speed, 110)
        self._menu.chosen.connect(self.set_speed)
        anchor = self.transport.speed_button
        self._menu.open_above(
            QRect(anchor.mapToGlobal(anchor.rect().topLeft()), anchor.size())
        )

    def _open_export_menu(self) -> None:
        self._close_menu()
        unavailable = export_availability(self.state.trimmed)
        real_h264 = system_ffmpeg() is not None
        rows = []
        for fid, _glyph, label, note, _rate in tokens.EXPORT_FORMATS:
            reason = unavailable.get(fid, "")
            size = "" if reason else f"{estimate_size_mb(fid, self.state.kept)} MB"
            if fid == "mp4":
                # The row promises what will actually be written. With a
                # system ffmpeg that is H.264, which is the handoff's own
                # wording; without one it is MPEG-4 Part 2, which does not
                # play in a browser and must not claim to.
                label, note = (
                    ("MP4 (H.264)", note)
                    if real_h264
                    else ("MP4 (MPEG-4)", "No H.264 encoder here. Desktop players only.")
                )
            rows.append((fid, label, note, size, reason))
        self._menu = FlowMenu(
            rows, self._format, 298, footnote=tokens.EXPORT_FOOTNOTE
        )
        self._menu.chosen.connect(self._choose_format)
        anchor = self.export_button
        self._menu.open_above(
            QRect(anchor.mapToGlobal(anchor.rect().topLeft()), anchor.size())
        )

    def _choose_format(self, format_id: str) -> None:
        self._format = format_id
        self._sync_chrome()

    # -- keyboard ----------------------------------------------------------

    def keyPressEvent(self, event) -> None:
        key = event.key()
        if key == Qt.Key.Key_Escape and self._menu is not None:
            self._close_menu()
            return
        actions = {
            Qt.Key.Key_Space: self.toggle_play,
            Qt.Key.Key_I: self.mark_in,
            Qt.Key.Key_O: self.mark_out,
            Qt.Key.Key_M: self.toggle_mute,
            Qt.Key.Key_L: self.toggle_loop,
            Qt.Key.Key_Left: lambda: self.step_frame(-1),
            Qt.Key.Key_Right: lambda: self.step_frame(1),
        }
        action = actions.get(key)
        if action is None:
            super().keyPressEvent(event)
            return
        action()
        event.accept()

    def closeEvent(self, event) -> None:
        # Stopped explicitly: a QMediaPlayer left playing keeps its decoder
        # and its audio device alive after the window is gone.
        self.player.stop()
        self._close_menu()
        super().closeEvent(event)

    # -- the footer's three actions ---------------------------------------

    def _display_path(self) -> str:
        try:
            return "~/" + str(self.path.relative_to(Path.home()))
        except ValueError:
            return str(self.path)

    def _destination_for(self, format_id: str) -> Path:
        suffix = {"frame": ".png", "mp4": ".mp4", "webm": ".webm"}.get(format_id, ".mp4")
        stem = self.path.stem
        if self.state.trimmed and format_id != "frame":
            # A trimmed export is a different clip from the recording, so it
            # gets a different name -- PRESERVE_ORIGINAL means never writing
            # over what was recorded. A still is exempt: one frame is not
            # "trimmed", and a PNG called "(trimmed)" describes something
            # the file is not.
            stem = f"{stem} (trimmed)"
        candidate = self.path.with_name(stem + suffix)
        index = 2
        while candidate.exists() and candidate != self.path:
            candidate = self.path.with_name(f"{stem} {index}{suffix}")
            index += 1
        return candidate

    def run_export(self) -> None:
        self._close_menu()
        reason = export_availability(self.state.trimmed).get(self._format)
        if reason:
            self._report(reason, ok=False)
            return

        destination = self._destination_for(self._format)
        if self._format == "frame":
            try:
                written = export_frame(self.canvas.current_frame(), destination)
            except (ValueError, OSError) as error:
                self._report(str(error), ok=False)
                return
            self._finish_export(written, mark_saved=False)
            return

        if self._format == "webm" and not self.state.trimmed:
            # Nothing to change: re-encoding an untrimmed recording would
            # cost time and a generation of quality to produce the same
            # clip. True with or without ffmpeg.
            written = export_copy(self.path, destination)
            self._finish_export(written)
            return

        self._report("Exporting… 0%", ok=True, busy=True)
        binary = system_ffmpeg()
        if binary is not None:
            self._exporter = FfmpegExporter(
                self.path,
                destination,
                self.state,
                self._format,
                muted=self._muted,
                binary=binary,
                fps=self._source_fps,
                parent=self,
            )
        else:
            self._exporter = Exporter(
                self.path, destination, self.state, muted=self._muted, parent=self
            )
        self._exporter.progressed.connect(
            lambda fraction: self._report(
                f"Exporting… {int(fraction * 100)}%", ok=True, busy=True
            )
        )
        self._exporter.finished.connect(self._finish_export)
        self._exporter.failed.connect(lambda why: self._report(why, ok=False))
        self._exporter.start()

    def _finish_export(self, written: str, mark_saved: bool = True) -> None:
        # A still is not the clip: exporting one frame does not mean the
        # trim has been written anywhere, so it must not clear the dirty
        # mark.
        if mark_saved:
            self._saved = True
        self._sync_chrome()
        self._report(f"Saved to {Path(written).name}", ok=True)
        self.exported.emit(written)

    def _set_status(self, message: str, *, ok: bool) -> None:
        colour = _C.SAVED_FG if ok else _C.DIRTY_FG
        self.status_label.setText(message)
        self.status_label.setStyleSheet(f"color: {colour};")
        # Same reason as the trim readout: without a claimed width the
        # footer's stretch squeezed "Edited — not exported" down to
        # "Edited", which says something quite different.
        self.status_label.setMinimumWidth(self.status_label.sizeHint().width())
        self.status_icon.setPixmap(
            design.icon("check" if ok else "pen", QColor(colour)).pixmap(QSize(14, 14))
        )

    def _report(self, message: str, *, ok: bool, busy: bool = False) -> None:
        self._set_status(message, ok=ok)
        if not busy and ok:
            QTimer.singleShot(4000, self._sync_chrome)

    def copy_file(self) -> None:
        """A file *reference*, not the bytes.

        Pastes into a chat, an upload field or a file manager, and does
        nothing in an image editor -- the same rule the capture flow's Copy
        follows for a recording.
        """
        from PyQt6.QtCore import QMimeData

        data = QMimeData()
        url = QUrl.fromLocalFile(str(self.path))
        data.setUrls([url])
        data.setText(str(self.path))
        QGuiApplication.clipboard().setMimeData(data)
        self._report(f"Copied {self.path.name}", ok=True)

    def show_in_folder(self) -> None:
        from PyQt6.QtGui import QDesktopServices

        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.path.parent)))


class _RowButton(QWidget):
    """One of the trim row's three 26px controls.

    `edge` puts a 2px accent bar on the side the button affects -- left for
    `Start here`, right for `End here` -- so the pair reads as a range
    without either label having to say "of the clip".
    """

    clicked = pyqtSignal()

    def __init__(
        self,
        text: str,
        shortcut: str = "",
        *,
        edge: str = "",
        flat: bool = False,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self._text = text
        self._shortcut = shortcut
        self._edge = edge
        self._flat = flat
        self._hover = False
        self._muted = False
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(_M.ROW_BTN_H)
        metrics = QFontMetrics(_ui_font(11.5, 500))
        width = 20 + metrics.horizontalAdvance(text)
        if edge:
            width += 8
        if shortcut:
            width += 6 + QFontMetrics(_mono_font(10)).horizontalAdvance(shortcut)
        self.setFixedWidth(width)

    def set_muted_look(self, muted: bool) -> None:
        """Reset greys out when there is nothing to reset."""
        self._muted = muted
        self.update()

    def enterEvent(self, event) -> None:
        self._hover = True
        self.update()

    def leaveEvent(self, event) -> None:
        self._hover = False
        self.update()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if self.rect().contains(event.position().toPoint()):
            self.clicked.emit()
        event.accept()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        body = QRectF(0.5, 0.5, self.width() - 1, self.height() - 1)
        if self._flat:
            background = QColor(tokens.Win.ROW_HOVER) if self._hover else QColor(
                Qt.GlobalColor.transparent
            )
            border = QColor(tokens.Win.FIELD_BORDER)
        else:
            background = QColor(
                tokens.Win.CONTROL_BG_HOVER if self._hover else tokens.Win.CONTROL_BG
            )
            border = QColor(tokens.Win.CONTROL_BORDER)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(background)
        painter.drawRoundedRect(body, 7, 7)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(border)
        painter.drawRoundedRect(body, 7, 7)

        x = 10.0
        if self._edge == "left":
            painter.fillRect(
                QRectF(x, (self.height() - 11) / 2, 2, 11), QColor(_C.TRIM)
            )
            x += 8

        if self._muted:
            colour = QColor(tokens.Win.TEXT_DISABLED)
        elif self._flat:
            colour = QColor(tokens.Win.TEXT_SECONDARY)
        else:
            colour = QColor(tokens.Win.TEXT_BODY)
        painter.setFont(_ui_font(11.5, 500))
        painter.setPen(colour)
        metrics = QFontMetrics(painter.font())
        width = metrics.horizontalAdvance(self._text)
        painter.drawText(
            QRectF(x, 0, width, self.height()),
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            self._text,
        )
        x += width

        if self._edge == "right":
            painter.fillRect(
                QRectF(x + 6, (self.height() - 11) / 2, 2, 11), QColor(_C.TRIM)
            )
            x += 8

        if self._shortcut:
            painter.setFont(_mono_font(10))
            painter.setPen(QColor(tokens.Win.TEXT_FAINT))
            painter.drawText(
                QRectF(x + 6, 0, self.width() - x - 6, self.height()),
                int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                self._shortcut,
            )
        painter.end()


class _TrimReadout(QLabel):
    """The plain-language answer to "what am I about to export".

    Rich text rather than four labels: the sentence is one thing to read,
    and the two coloured figures -- what is kept, what is cut -- are the
    only parts that need their own colour.
    """

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setFont(_mono_font(11.5))
        self.setStyleSheet(f"color: {tokens.Win.TEXT_MUTED}; border: none;")

    def set_state(self, state: TrimState) -> None:
        strong = tokens.Win.TEXT_PRIMARY
        rich = [
            f"in <span style='color:{strong}'>{format_timecode(state.start)}</span>",
            f"out <span style='color:{strong}'>{format_timecode(state.end)}</span>",
            f"keeping <span style='color:{_C.KEPT_FG}'>{format_clock(state.kept)}</span>"
            f" of {format_clock(state.duration)}",
        ]
        plain = [
            f"in {format_timecode(state.start)}",
            f"out {format_timecode(state.end)}",
            f"keeping {format_clock(state.kept)} of {format_clock(state.duration)}",
        ]
        if state.cut_is_reportable:
            # Only when there is a figure to report -- see
            # `TrimState.cut_is_reportable`.
            rich.append(
                f"<span style='color:{_C.CUT_FG}'>−{format_clock(state.cut)} cut</span>"
            )
            plain.append(f"−{format_clock(state.cut)} cut")

        self.setText("  ·  ".join(rich))
        # Claimed rather than requested, and measured off the *plain* text.
        # Two separate traps: this label sits after a stretch, so one that
        # merely prefers its hint loses the argument; and `sizeHint()` on
        # rich text under-reports -- by 43px here -- which clipped the
        # "cut" clause clean off the end, the one figure the line exists to
        # report.
        metrics = QFontMetrics(self.font())
        self.setFixedWidth(metrics.horizontalAdvance("  ·  ".join(plain)) + 6)
