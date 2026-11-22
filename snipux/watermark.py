"""The watermark's own pieces: the session's choice, what Settings says the
mark is, and the menu that picks its corner and strength.

`WatermarkChoice` is the shape of the on/off, corner and opacity the stills
bar carries from one snip to the next; `watermark_content` and
`watermark_text_style` read what the mark is from Settings; `WatermarkMenu`
is what the bar's watermark notch opens. The mark itself is
`shapes.Watermark`.

Separate from `overlay.py` because none of it is about the frozen frame: it
is one feature's settings and its one menu, and it changes when the
watermark does. The running session's `WatermarkChoice` -- the instance,
`overlay.watermark_session` -- stays with the overlay, its only reader.
Nothing in this module imports `overlay.py`.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PyQt6.QtCore import QRect, QRectF, QSize, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QImage, QPainter, QPen
from PyQt6.QtWidgets import (
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QSlider,
    QVBoxLayout,
)

from snipux import design, glass, setup_desktop
from snipux.toolbar import _Chrome


# ---------------------------------------------------------------------------
# The watermark (#69)
# ---------------------------------------------------------------------------
# docs/design/bars/README.md, "Watermark is not a tool": a toggle on the
# stills bar, a notch for where the mark goes and how strongly, and what the
# mark is in Settings. The mark itself is `shapes.Watermark`, which the live
# preview and the export both paint.


@dataclass
class WatermarkChoice:
    """The stills bar's watermark: on or off, which corner, what opacity.

    A session's, not a preference's. It carries from one snip to the next
    for as long as snipux runs, and a fresh launch starts it off, in the
    bottom-right corner at 70% (#69). What the mark is lives in Settings --
    see `watermark_content`.
    """

    enabled: bool = False
    corner: str = design.tokens.WATERMARK["default_corner"]
    opacity: int = design.tokens.WATERMARK["default_opacity"]


def watermark_content(config_dir: Path | None = None) -> "tuple[str | QImage | None, str]":
    """What Settings says the watermark stamps: `(text or image, "")`, or
    `(None, reason)` when there is nothing it can stamp.

    Read at the start of every snip, so a change in Settings reaches the
    very next one. Nothing here raises: a value that cannot be used is a
    reason for the slot to grey with, never a capture that fails.
    """
    tokens = design.tokens
    if setup_desktop.load_watermark_kind(config_dir) == "image":
        if setup_desktop.load_watermark_image_name(config_dir) is None:
            return None, tokens.WATERMARK_UNSET
        path = setup_desktop.load_watermark_image(config_dir)
        if path is None:
            return None, tokens.WATERMARK_IMAGE_MISSING
        image = QImage(str(path))
        if image.isNull():
            return None, tokens.WATERMARK_IMAGE_UNREADABLE
        return image, ""
    text = setup_desktop.load_watermark_text(config_dir)
    return (text, "") if text else (None, tokens.WATERMARK_UNSET)


@dataclass(frozen=True)
class WatermarkTextStyle:
    """How a text mark looks, as Settings has it: read with the content at
    the start of every snip, so the preview and the export share one."""

    color: QColor
    font_family: str
    backing: bool


def watermark_text_style(config_dir: Path | None = None) -> WatermarkTextStyle:
    return WatermarkTextStyle(
        color=QColor(setup_desktop.load_watermark_color(config_dir)),
        font_family=setup_desktop.load_watermark_font(config_dir),
        backing=setup_desktop.load_watermark_backing(config_dir),
    )


def _watermark_menu_font(spec: tuple[float, int], mono: bool = False) -> QFont:
    families = design.font_families()
    font = QFont(families.mono if mono else families.ui)
    size, weight = spec
    font.setPixelSize(round(size))
    font.setWeight(QFont.Weight(weight))
    return font


class _CornerButton(QPushButton):
    """One of the watermark menu's four corners: a small capture with the
    mark drawn in that corner, so the choice is shown rather than spelled
    out. Its name is the tooltip.
    """

    def __init__(self, corner: str, name: str, parent=None):
        super().__init__(parent)
        # Keys belong to the overlay -- see _CloseButton.
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.corner = corner
        self._selected = False
        self._hovered = False
        self.setFlat(True)
        self.setStyleSheet("QPushButton { border: none; background: transparent; }")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMouseTracking(True)
        self.setToolTip(name)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setFixedHeight(design.tokens.WatermarkMetric.CORNER_H)

    def sizeHint(self) -> QSize:
        # `QPushButton`'s own hint measures a text and an icon this button
        # never sets. The grid shares the menu's width between the two.
        metric = design.tokens.WatermarkMetric
        return QSize(metric.CORNER_FRAME[0] + 2 * metric.CORNER_DOT_INSET, metric.CORNER_H)

    def minimumSizeHint(self) -> QSize:
        return self.sizeHint()

    @property
    def is_selected(self) -> bool:
        return self._selected

    def set_selected(self, selected: bool) -> None:
        self._selected = selected
        self.update()

    def enterEvent(self, event) -> None:
        self._hovered = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._hovered = False
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        metric = design.tokens.WatermarkMetric
        colour = design.watermark_color

        if self._selected:
            fill = colour("CORNER_ON_BG")
        elif self._hovered:
            fill = colour("CORNER_HOVER_BG")
        else:
            fill = None
        painter.setPen(QPen(colour("CORNER_ON_BORDER" if self._selected else "CORNER_BORDER"), 1))
        painter.setBrush(fill if fill is not None else Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(
            QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5),
            metric.CORNER_RADIUS,
            metric.CORNER_RADIUS,
        )

        frame_w, frame_h = metric.CORNER_FRAME
        frame = QRectF(
            (self.width() - frame_w) / 2, (self.height() - frame_h) / 2, frame_w, frame_h
        )
        painter.setPen(QPen(colour("CORNER_FRAME"), 1))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(
            frame.adjusted(0.5, 0.5, -0.5, -0.5),
            metric.CORNER_FRAME_RADIUS,
            metric.CORNER_FRAME_RADIUS,
        )

        dot_w, dot_h = metric.CORNER_DOT
        # Measured inside the frame's 1px edge, where the spec's markup
        # positions it.
        inset = metric.CORNER_DOT_INSET + 1
        left = frame.left() + inset if self.corner in ("tl", "bl") else frame.right() - inset - dot_w
        top = frame.top() + inset if self.corner in ("tl", "tr") else frame.bottom() - inset - dot_h
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(colour("CORNER_DOT_ON" if self._selected else "CORNER_DOT"))
        painter.drawRoundedRect(
            QRectF(left, top, dot_w, dot_h), metric.CORNER_DOT_RADIUS, metric.CORNER_DOT_RADIUS
        )
        painter.end()


class WatermarkMenu(_Chrome):
    """The watermark slot's menu: which corner the mark goes in, and how
    strongly it shows.

    It stays open while either changes -- watching the preview move behind
    it is the point of changing them -- and closes the way the family menus
    do: a press outside it, Esc, or another of the bar's menus opening. A
    child of the window the bar sits over, for the reasons `FamilyMenu`
    gives.
    """

    cornerPicked = pyqtSignal(str)
    opacityChanged = pyqtSignal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        tokens = design.tokens
        metric = tokens.WatermarkMetric
        fonts = tokens.WatermarkFont
        pad_v, pad_h = metric.MENU_PAD
        border = tokens.BarMetric.BORDER
        # Border-box, as the handoff insists: the width is the menu's whole
        # outside edge, padding and border included.
        self.setFixedWidth(metric.MENU_W)
        content_w = metric.MENU_W - 2 * (pad_h + border)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(pad_h + border, pad_v + border, pad_h + border, pad_v + border)
        layout.setSpacing(metric.MENU_SECTION_GAP)

        corners = QVBoxLayout()
        corners.setContentsMargins(0, 0, 0, 0)
        corners.setSpacing(metric.MENU_LABEL_GAP)
        heading = QLabel("Corner".upper(), self)
        heading_font = _watermark_menu_font(fonts.SECTION)
        heading_font.setLetterSpacing(
            QFont.SpacingType.AbsoluteSpacing, fonts.SECTION_TRACKING * fonts.SECTION[0]
        )
        heading.setFont(heading_font)
        heading.setStyleSheet(f"color: {design.watermark_color('SECTION_FG').name()};")
        corners.addWidget(heading)

        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(metric.CORNER_GAP)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        self._corner_buttons: dict[str, _CornerButton] = {}
        for index, (corner, name) in enumerate(tokens.WATERMARK_CORNERS):
            button = _CornerButton(corner, name, self)
            button.clicked.connect(lambda checked=False, c=corner: self._on_corner_clicked(c))
            self._corner_buttons[corner] = button
            grid.addWidget(button, index // 2, index % 2)
        corners.addLayout(grid)
        layout.addLayout(corners)

        opacity = QHBoxLayout()
        opacity.setContentsMargins(0, 0, 0, 0)
        opacity.setSpacing(metric.OPACITY_GAP)
        label = QLabel("Opacity", self)
        label.setFont(_watermark_menu_font(fonts.LABEL))
        label.setStyleSheet(f"color: {design.watermark_color('LABEL_FG').name()};")
        opacity.addWidget(label)
        self._slider = QSlider(Qt.Orientation.Horizontal, self)
        low, high = tokens.WATERMARK["opacity_range"]
        self._slider.setRange(low, high)
        self._slider.setValue(tokens.WATERMARK["default_opacity"])
        self._slider.setToolTip("Watermark opacity")
        self._slider.valueChanged.connect(self._on_slider_moved)
        opacity.addWidget(self._slider, 1)
        self._readout = QLabel(self)
        self._readout.setFont(_watermark_menu_font(fonts.READOUT, mono=True))
        self._readout.setStyleSheet(f"color: {design.watermark_color('READOUT_FG').name()};")
        self._readout.setMinimumWidth(metric.READOUT_MIN_W)
        self._readout.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        opacity.addWidget(self._readout)
        layout.addLayout(opacity)

        self._note = QLabel(tokens.WATERMARK_MENU_NOTE, self)
        self._note.setWordWrap(True)
        self._note.setFont(_watermark_menu_font(fonts.NOTE))
        self._note.setStyleSheet(f"color: {design.watermark_color('NOTE_FG').name()};")
        # Fixed at the height its wrapped lines take at the menu's own width:
        # a wrapping label reports one line's height to a layout that has not
        # given it a width yet, and the menu would come out a line short.
        self._note.setFixedWidth(content_w)
        self._note.setFixedHeight(self._note.heightForWidth(content_w))
        layout.addWidget(self._note)

        self._corner = tokens.WATERMARK["default_corner"]
        self.set_corner(self._corner)
        self._refresh_readout()
        self.resize(self.sizeHint())

    @property
    def corner(self) -> str:
        return self._corner

    @property
    def opacity(self) -> int:
        return self._slider.value()

    def set_corner(self, corner: str) -> None:
        """Mark `corner` chosen without reporting it -- for seeding the menu
        from the session before it opens."""
        self._corner = corner
        for name, button in self._corner_buttons.items():
            button.set_selected(name == corner)

    def set_opacity(self, opacity: int) -> None:
        """Set the slider without reporting it, as `set_corner` does."""
        self._slider.blockSignals(True)
        self._slider.setValue(opacity)
        self._slider.blockSignals(False)
        self._refresh_readout()

    def _on_corner_clicked(self, corner: str) -> None:
        self.set_corner(corner)
        self.cornerPicked.emit(corner)

    def _on_slider_moved(self, value: int) -> None:
        self._refresh_readout()
        self.opacityChanged.emit(value)

    def _refresh_readout(self) -> None:
        self._readout.setText(f"{self._slider.value()}%")

    def reposition(self, slot: QRect, bar: QRect, bounds: QRectF) -> None:
        """Right-aligned to `slot`, `MENU_OVERHANG` past its right edge, and
        above `bar`, `MENU_OFFSET` clear of it -- below the bar instead when
        there is no room above inside `bounds`. All three are in the
        parent's coordinates.

        Right-aligned where the family menus centre, because the spec
        anchors this one to its slot's right edge.
        """
        metric = design.tokens.WatermarkMetric
        offset = design.tokens.BarMetric.MENU_OFFSET
        height = self.sizeHint().height()
        width = self.width()
        left = QRectF(slot).right() + metric.MENU_OVERHANG - width
        left = max(bounds.left(), min(left, bounds.right() - width))
        bar_rect = QRectF(bar)
        top = bar_rect.top() - offset - height
        if top < bounds.top():
            top = min(bar_rect.bottom() + offset, bounds.bottom() - height)
        self.setGeometry(round(left), round(top), width, height)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        radius = design.tokens.WatermarkMetric.MENU_RADIUS
        path = glass.rounded(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), radius)
        self.glass.paint(painter, path, design.bar_color("MENU_BG"))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(design.bar_color("MENU_BORDER"))
        painter.drawPath(path)
        painter.end()
