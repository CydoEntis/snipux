"""The made-up desktop every marketing shot is taken over.

Painted with QPainter at a fixed logical size and scale: a code editor, a
revenue chart and a team chat over a gradient wallpaper, with a taskbar
whose clock always reads the same time. Text is drawn in whatever fonts the
machine resolves, so render on Windows for the published set. All of it is fiction -- the people, the company and the
address the chat mentions (example.com is reserved for exactly this).

`build_scene` hands back the pixels plus where it drew the things a shot
needs to aim at, in logical desktop coordinates, so the script never has to
guess where a word landed.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from PyQt6.QtCore import QPointF, QRectF, QSizeF, Qt
from PyQt6.QtGui import (
    QColor,
    QFont,
    QFontMetricsF,
    QImage,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QRadialGradient,
)

from snipux.capture import Frame
from snipux.design import font_families

LOGICAL_SIZE = QSizeF(1600, 1000)
SCALE = 2.0

CLOCK = "9:41"
TASKBAR_H = 48.0

EDITOR = QRectF(48, 56, 800, 640)
REVENUE = QRectF(904, 56, 580, 412)
CHAT = QRectF(904, 490, 580, 340)

EMAIL = "marco@example.com"

# The editor's code as (text, colour) runs per line. The palette is a
# One Dark-ish one: readable at thumbnail size, not any real theme's.
_KW, _FN, _STR, _NUM, _COM, _TXT, _TY = (
    "#c678dd", "#61afef", "#98c379", "#d19a66", "#6b7383", "#d7dae0", "#e5c07b",
)
CODE = [
    [("# Weekly revenue summary for the ops dashboard", _COM)],
    [("from ", _KW), ("metrics ", _TXT), ("import ", _KW), ("load_orders, by_region", _TXT)],
    [],
    [("def ", _KW), ("weekly_summary", _FN), ("(week: ", _TXT), ("int", _TY), (") -> ", _TXT), ("dict", _TY), (":", _TXT)],
    [("    orders = load_orders(week)", _TXT)],
    [("    total = ", _TXT), ("sum", _FN), ("(o.amount ", _TXT), ("for ", _KW), ("o ", _TXT), ("in ", _KW), ("orders)", _TXT)],
    [("    regions = by_region(orders)", _TXT)],
    [("    if ", _KW), ("total < ", _TXT), ("10_000", _NUM), (":", _TXT)],
    [("        alert(", _TXT), ('"Revenue below target"', _STR), (")", _TXT)],
    [("    return ", _KW), ("{", _TXT)],
    [("        ", _TXT), ('"total"', _STR), (": total,", _TXT)],
    [("        ", _TXT), ('"regions"', _STR), (": regions,", _TXT)],
    [("        ", _TXT), ('"growth"', _STR), (": growth(orders, ", _TXT), ("4", _NUM), ("),", _TXT)],
    [("    }", _TXT)],
    [],
    [("def ", _KW), ("growth", _FN), ("(orders, weeks: ", _TXT), ("int", _TY), (") -> ", _TXT), ("float", _TY), (":", _TXT)],
    [("    recent = orders[-weeks:]", _TXT)],
    [("    return ", _KW), ("(recent[-", _TXT), ("1", _NUM), ("] - recent[", _TXT), ("0", _NUM), ("]) / recent[", _TXT), ("0", _NUM), ("]", _TXT)],
]
# The line a recording clip "types", one character per step.
TYPED_LINE = [("    ", _TXT), ("log", _FN), (".info(", _TXT), ('"summary ready"', _STR), (")", _TXT)]

BARS = [0.42, 0.55, 0.50, 0.63, 0.58, 0.70, 0.81, 0.76, 0.88, 1.0]

MESSAGES = [
    ("Priya", "#f59e0b", "9:32", "Numbers are in, revenue is up 12%!"),
    ("Marco", "#22c55e", "9:35", f"Send the invoice to {EMAIL}"),
    ("Priya", "#f59e0b", "9:38", "Grabbing a screenshot for the deck now"),
]


@dataclass
class Scene:
    """A painted desktop and the logical rects of what is on it."""

    image: QImage
    logical_size: QSizeF
    # name -> rect, in logical desktop coordinates.
    places: dict[str, QRectF] = field(default_factory=dict)
    # The chat as OCR would read it: one list of (word, logical rect) per line.
    chat_lines: list[list[tuple[str, QRectF]]] = field(default_factory=list)

    def frame(self) -> Frame:
        return Frame(image=self.image, logical_origin=QPointF(0, 0), logical_size=self.logical_size)


def _font(px: float, weight: int = 400, *, mono: bool = False) -> QFont:
    families = font_families()
    font = QFont(families.mono if mono else families.ui)
    font.setPixelSize(round(px))
    font.setWeight(QFont.Weight(weight))
    return font


def build_scene(*, typed: int | None = None, bar_growth: float = 1.0) -> Scene:
    """Paint the desktop at `SCALE`.

    `typed` adds that many characters of `TYPED_LINE` under the code, with a
    caret -- what a recording clip films changing. `bar_growth` scales the
    newest bar of the chart the same way.
    """
    image = QImage(
        round(LOGICAL_SIZE.width() * SCALE),
        round(LOGICAL_SIZE.height() * SCALE),
        QImage.Format.Format_RGB32,
    )
    image.setDevicePixelRatio(SCALE)
    scene = Scene(image=image, logical_size=LOGICAL_SIZE)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
    try:
        _paint_wallpaper(painter)
        _paint_editor(painter, scene, typed)
        _paint_revenue(painter, scene, bar_growth)
        _paint_chat(painter, scene)
        _paint_taskbar(painter, scene)
    finally:
        painter.end()
    # Frame.crop works in raw pixels and derives its own ratio; a ratio
    # left on the image would make Qt paint it at half size downstream.
    image.setDevicePixelRatio(1.0)
    return scene


def _paint_wallpaper(painter: QPainter) -> None:
    full = QRectF(QPointF(0, 0), LOGICAL_SIZE)
    base = QLinearGradient(full.topLeft(), full.bottomRight())
    base.setColorAt(0.0, QColor("#141a33"))
    base.setColorAt(0.55, QColor("#2a1f4d"))
    base.setColorAt(1.0, QColor("#5b2350"))
    painter.fillRect(full, base)
    for centre, radius, colour in (
        (QPointF(1250, 180), 700, QColor(124, 92, 255, 70)),
        (QPointF(300, 900), 650, QColor(56, 189, 248, 38)),
        (QPointF(1500, 950), 500, QColor(236, 72, 153, 60)),
    ):
        glow = QRadialGradient(centre, radius)
        glow.setColorAt(0.0, colour)
        glow.setColorAt(1.0, QColor(colour.red(), colour.green(), colour.blue(), 0))
        painter.fillRect(full, glow)


def _window(painter: QPainter, rect: QRectF, title: str, *, light: bool = False) -> QRectF:
    """A rounded app window with shadow and title bar; returns its body rect."""
    radius = 12.0
    for i in range(14, 0, -1):
        shade = QColor(0, 0, 0, int(5 + (14 - i) * 1.2))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(shade)
        painter.drawRoundedRect(rect.adjusted(-i, -i + 10, i, i + 10), radius + i, radius + i)
    body_colour = QColor("#f7f8fc") if light else QColor("#1f2229")
    bar_colour = QColor("#eceef5") if light else QColor("#2a2e37")
    path = QPainterPath()
    path.addRoundedRect(rect, radius, radius)
    painter.save()
    painter.setClipPath(path)
    painter.fillRect(rect, body_colour)
    bar = QRectF(rect.left(), rect.top(), rect.width(), 40)
    painter.fillRect(bar, bar_colour)
    painter.fillRect(QRectF(bar.left(), bar.bottom() - 1, bar.width(), 1),
                     QColor(0, 0, 0, 30 if light else 90))
    painter.restore()
    painter.setPen(QPen(QColor(255, 255, 255, 18 if not light else 0), 1))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5), radius, radius)

    painter.setPen(QColor("#5b6070") if light else QColor("#aab0bf"))
    painter.setFont(_font(13, 500))
    painter.drawText(QRectF(bar.left() + 18, bar.top(), bar.width() - 140, bar.height()),
                     Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, title)
    for i, colour in enumerate(("#9aa0ad", "#9aa0ad", "#ef5350")):
        centre = QPointF(bar.right() - 70 + i * 24, bar.center().y())
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(colour))
        painter.drawEllipse(centre, 5.5, 5.5)
    return QRectF(rect.left(), bar.bottom(), rect.width(), rect.height() - bar.height())


def _runs(painter: QPainter, x: float, baseline: float, runs) -> float:
    metrics = QFontMetricsF(painter.font())
    for text, colour in runs:
        painter.setPen(QColor(colour))
        painter.drawText(QPointF(x, baseline), text)
        x += metrics.horizontalAdvance(text)
    return x


def _paint_editor(painter: QPainter, scene: Scene, typed: int | None) -> None:
    body = _window(painter, EDITOR, "dashboard.py — acme-analytics")
    scene.places["editor"] = QRectF(EDITOR)
    side = QRectF(body.left(), body.top(), 208, body.height() - 26)
    painter.fillRect(side, QColor("#1a1d23"))
    painter.setFont(_font(12, 600))
    painter.setPen(QColor("#7d8496"))
    painter.drawText(QPointF(side.left() + 18, side.top() + 28), "EXPLORER")
    files = ["acme-analytics", "charts.py", "dashboard.py", "metrics.py", "report.py", "tests", "README.md"]
    painter.setFont(_font(13))
    for i, name in enumerate(files):
        y = side.top() + 46 + i * 28
        if name == "dashboard.py":
            painter.fillRect(QRectF(side.left(), y, side.width(), 28), QColor("#2c313c"))
        painter.setPen(QColor("#e6e8ee") if name == "dashboard.py" else QColor("#9aa1b1"))
        indent = 18 if i == 0 else 36
        label = ("▾  " + name) if i == 0 else name
        painter.drawText(QRectF(side.left() + indent, y, side.width(), 28),
                         Qt.AlignmentFlag.AlignVCenter, label)

    tabs = QRectF(side.right(), body.top(), body.right() - side.right(), 36)
    painter.fillRect(tabs, QColor("#1a1d23"))
    tab = QRectF(tabs.left(), tabs.top(), 150, tabs.height())
    painter.fillRect(tab, QColor("#1f2229"))
    painter.fillRect(QRectF(tab.left(), tab.top(), tab.width(), 2), QColor("#61afef"))
    painter.setPen(QColor("#e6e8ee"))
    painter.setFont(_font(13))
    painter.drawText(tab, Qt.AlignmentFlag.AlignCenter, "dashboard.py")
    painter.setPen(QColor("#7d8496"))
    painter.drawText(QRectF(tab.right(), tabs.top(), 130, tabs.height()),
                     Qt.AlignmentFlag.AlignCenter, "metrics.py")

    painter.setFont(_font(14, mono=True))
    line_h = 27.0
    top = tabs.bottom() + 30
    lines = list(CODE)
    if typed is not None:
        text = "".join(t for t, _c in TYPED_LINE)[:typed]
        lines.append(_clip_runs(TYPED_LINE, len(text)))
    for number, runs in enumerate(lines, start=1):
        baseline = top + (number - 1) * line_h
        painter.setPen(QColor("#4b5263"))
        painter.drawText(QRectF(side.right() + 8, baseline - 18, 36, 24),
                         Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, str(number))
        end = _runs(painter, side.right() + 64, baseline, runs)
        if typed is not None and number == len(lines):
            painter.fillRect(QRectF(end + 1, baseline - 15, 2, 19), QColor("#61afef"))

    status = QRectF(body.left(), body.bottom() - 26, body.width(), 26)
    painter.fillRect(status, QColor("#2a2e37"))
    painter.setFont(_font(12))
    painter.setPen(QColor("#aab0bf"))
    painter.drawText(status.adjusted(16, 0, 0, 0), Qt.AlignmentFlag.AlignVCenter, "⎇  main    ✓ 0 problems")
    painter.drawText(status.adjusted(0, 0, -16, 0),
                     Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight,
                     "Ln 18, Col 42    Spaces: 4    UTF-8    Python")


def _clip_runs(runs, count: int):
    out = []
    for text, colour in runs:
        if count <= 0:
            break
        out.append((text[:count], colour))
        count -= len(text)
    return out


def _paint_revenue(painter: QPainter, scene: Scene, bar_growth: float) -> None:
    body = _window(painter, REVENUE, "Acme Analytics — Revenue", light=True)
    scene.places["revenue"] = QRectF(REVENUE)
    left = body.left() + 32
    painter.setPen(QColor("#4a5063"))
    painter.setFont(_font(16, 600))
    painter.drawText(QPointF(left, body.top() + 42), "Weekly revenue")
    painter.setPen(QColor("#141826"))
    painter.setFont(_font(44, 700))
    headline = "$48,210"
    painter.drawText(QPointF(left, body.top() + 98), headline)
    head_w = QFontMetricsF(painter.font()).horizontalAdvance(headline)
    # The number and its pill, clear of the label above them.
    scene.places["headline"] = QRectF(left - 10, body.top() + 58, head_w + 202, 52)
    pill = QRectF(left + head_w + 22, body.top() + 66, 158, 30)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor("#dcfce7"))
    painter.drawRoundedRect(pill, 15, 15)
    painter.setPen(QColor("#15803d"))
    painter.setFont(_font(13, 600))
    painter.drawText(pill, Qt.AlignmentFlag.AlignCenter, "▲ 12.4% vs last week")

    chart = QRectF(left, body.top() + 132, body.width() - 64, body.height() - 180)
    painter.setPen(QPen(QColor("#e3e6ef"), 1))
    for i in range(4):
        y = chart.top() + chart.height() * i / 3
        painter.drawLine(QPointF(chart.left(), y), QPointF(chart.right(), y))
    gap = 14.0
    width = (chart.width() - gap * (len(BARS) - 1)) / len(BARS)
    painter.setFont(_font(11))
    for i, value in enumerate(BARS):
        if i == len(BARS) - 1:
            value *= bar_growth
        h = chart.height() * value
        rect = QRectF(chart.left() + i * (width + gap), chart.bottom() - h, width, h)
        grad = QLinearGradient(rect.topLeft(), rect.bottomLeft())
        latest = i == len(BARS) - 1
        grad.setColorAt(0.0, QColor("#6d5dfc" if latest else "#8b93ff"))
        grad.setColorAt(1.0, QColor("#a78bfa" if latest else "#c7cbff"))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(grad)
        painter.drawRoundedRect(rect, 6, 6)
        if latest:
            scene.places["best_bar"] = rect
        painter.setPen(QColor("#8a90a2"))
        painter.drawText(QRectF(rect.left() - 6, chart.bottom() + 8, width + 12, 18),
                         Qt.AlignmentFlag.AlignCenter, f"W{i + 1}")


def _paint_chat(painter: QPainter, scene: Scene) -> None:
    body = _window(painter, CHAT, "Team chat — #launch")
    scene.places["chat"] = QRectF(CHAT)
    text_font = _font(15)
    metrics = QFontMetricsF(text_font)
    for i, (name, colour, time, text) in enumerate(MESSAGES):
        top = body.top() + 20 + i * 72
        avatar = QRectF(body.left() + 24, top, 40, 40)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(colour))
        painter.drawEllipse(avatar)
        painter.setPen(QColor("#111318"))
        painter.setFont(_font(17, 700))
        painter.drawText(avatar, Qt.AlignmentFlag.AlignCenter, name[0])
        x = avatar.right() + 16
        painter.setFont(_font(15, 700))
        painter.setPen(QColor("#f1f3f7"))
        painter.drawText(QPointF(x, top + 15), name)
        name_w = QFontMetricsF(painter.font()).horizontalAdvance(name)
        painter.setFont(_font(12))
        painter.setPen(QColor("#7d8496"))
        painter.drawText(QPointF(x + name_w + 10, top + 15), time)
        painter.setFont(text_font)
        painter.setPen(QColor("#c9cdd8"))
        baseline = top + 40
        painter.drawText(QPointF(x, baseline), text)
        words = []
        cursor = x
        for word in text.split(" "):
            w = metrics.horizontalAdvance(word)
            words.append((word, QRectF(cursor, baseline - metrics.ascent(), w, metrics.height())))
            cursor += w + metrics.horizontalAdvance(" ")
        scene.chat_lines.append(words)
        scene.places[f"message{i}"] = QRectF(x, baseline - metrics.ascent(), cursor - x, metrics.height())
        if EMAIL in text:
            scene.places["email"] = words[-1][1]

    box = QRectF(body.left() + 20, body.bottom() - 62, body.width() - 40, 42)
    painter.setPen(QPen(QColor("#3a3f4b"), 1))
    painter.setBrush(QColor("#262a33"))
    painter.drawRoundedRect(box, 10, 10)
    painter.setPen(QColor("#6b7283"))
    painter.setFont(_font(14))
    painter.drawText(box.adjusted(16, 0, 0, 0), Qt.AlignmentFlag.AlignVCenter, "Message #launch")


def _paint_taskbar(painter: QPainter, scene: Scene) -> None:
    bar = QRectF(0, LOGICAL_SIZE.height() - TASKBAR_H, LOGICAL_SIZE.width(), TASKBAR_H)
    painter.fillRect(bar, QColor(12, 13, 20, 215))
    painter.fillRect(QRectF(bar.left(), bar.top(), bar.width(), 1), QColor(255, 255, 255, 22))
    icons = ["#3b82f6", "#f59e0b", "#22c55e", "#ef4444", "#a855f7"]
    size, gap = 32.0, 12.0
    start = bar.center().x() - (len(icons) * size + (len(icons) - 1) * gap) / 2
    for i, colour in enumerate(icons):
        rect = QRectF(start + i * (size + gap), bar.top() + (TASKBAR_H - size) / 2, size, size)
        grad = QLinearGradient(rect.topLeft(), rect.bottomRight())
        grad.setColorAt(0, QColor(colour).lighter(125))
        grad.setColorAt(1, QColor(colour).darker(115))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(grad)
        painter.drawRoundedRect(rect, 8, 8)
        if i < 3:
            painter.setBrush(QColor(255, 255, 255, 170))
            painter.drawRoundedRect(QRectF(rect.center().x() - 6, rect.bottom() + 4, 12, 3), 1.5, 1.5)
    painter.setPen(QColor("#e6e8ee"))
    painter.setFont(_font(14, 500))
    clock = QRectF(bar.right() - 120, bar.top(), 100, bar.height())
    painter.drawText(clock, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight, CLOCK)
    scene.places["clock"] = clock
