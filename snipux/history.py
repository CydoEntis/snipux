"""The history panel: a narrow column down the right of the screen listing
what snipux recently copied and saved, large enough to recognise a snip by
its picture rather than its filename, and a click away from the clipboard.

Opened by clicking the tray icon (and from the tray menu, since
GNOME's indicator passes no clicks on). A view only: it reports a click as
`copy_requested`/`open_requested` and `app.py` decides what that means --
the same split flowbars.py keeps.

The two lists come from where they already live: `output.copy_history()`
(in memory, gone at quit) and the Recent list in `config.json`. Nothing
here keeps a list of its own.
"""

from __future__ import annotations

import datetime
from pathlib import Path

from PyQt6.QtCore import QRect, Qt, pyqtSignal
from PyQt6.QtGui import QGuiApplication, QCursor, QImage, QImageReader, QKeyEvent, QPixmap
from PyQt6.QtWidgets import (
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from . import design, output
from .design import tokens
from .winchrome import _mono_font, _ui_font, scrollbar_style

# Logical pixels.
PANEL_W = 340
_EDGE_GAP = 10          # between the panel and the screen's work-area edge
_PAD = 14
_CARD_GAP = 10
_CARD_PAD = 8
PREVIEW_MAX_H = 190
_TEXT_PREVIEW_LINES = 6
# Wide enough for the panel's preview at 2x, so a thumbnail made once serves
# both the panel and the tray menu's small icons.
_THUMB_EDGE = 640

VIDEO_SUFFIXES = {".mp4", ".webm", ".mkv", ".mov"}

_thumbnails: dict[tuple[Path, int], QImage | None] = {}


def saved_thumbnail(path: Path) -> QImage | None:
    """A small copy of the image at `path`, or None for anything that is not
    one (a recording, a file gone missing). Cached by modification time: the
    tray menu asks for these every time it opens, and decoding a full-screen
    PNG each time would make it visibly slow to appear."""
    try:
        stamp = path.stat().st_mtime_ns
    except OSError:
        return None
    key = (path, stamp)
    if key not in _thumbnails:
        image = QImage()
        if path.suffix.lower() not in VIDEO_SUFFIXES:
            image = QImageReader(str(path)).read()
        if image.isNull():
            _thumbnails[key] = None
        else:
            if image.width() > _THUMB_EDGE or image.height() > _THUMB_EDGE:
                image = image.scaled(
                    _THUMB_EDGE, _THUMB_EDGE,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            _thumbnails[key] = image
    return _thumbnails[key]


def _when(moment: datetime.datetime) -> str:
    if moment.date() == datetime.date.today():
        return moment.strftime("%H:%M")
    return moment.strftime("%b %d, %H:%M")


class _Card(QFrame):
    """One entry: a preview, a caption, and (for saved files) Open."""

    clicked = pyqtSignal()
    open_clicked = pyqtSignal()

    def __init__(self, caption: str, *, image: QImage | None = None,
                 text: str | None = None, placeholder: str | None = None,
                 can_open: bool = False, parent: QWidget | None = None):
        super().__init__(parent)
        win = tokens.Win
        self.setObjectName("card")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("Click to copy")
        self.setStyleSheet(
            f"QFrame#card {{ background: {win.CONTROL_BG};"
            f" border: 1px solid {win.CONTROL_BORDER}; border-radius: 9px; }}"
            f"QFrame#card:hover {{ background: {win.CONTROL_BG_HOVER};"
            f" border-color: {win.CONTROL_BORDER_HOVER}; }}"
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(_CARD_PAD, _CARD_PAD, _CARD_PAD, _CARD_PAD)
        layout.setSpacing(6)

        preview_w = PANEL_W - 2 * _PAD - 2 * _CARD_PAD - 2
        self.preview = QLabel()
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        if image is not None:
            ratio = self.devicePixelRatioF()
            pixmap = QPixmap.fromImage(image.scaled(
                round(preview_w * ratio), round(PREVIEW_MAX_H * ratio),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            ))
            pixmap.setDevicePixelRatio(ratio)
            self.preview.setPixmap(pixmap)
        elif text is not None:
            self.preview.setText(text)
            self.preview.setWordWrap(True)
            self.preview.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
            self.preview.setFont(_ui_font(12.5, 400))
            self.preview.setStyleSheet(f"color: {win.TEXT_BODY}; background: transparent;")
            # Measured from the font in use, not a fixed height: the line
            # count is what is meant, and fonts differ between machines.
            line = self.preview.fontMetrics().lineSpacing()
            self.preview.setMaximumHeight(line * _TEXT_PREVIEW_LINES)
        else:
            self.preview.setText(placeholder or "")
            self.preview.setFont(_ui_font(12.5, 500))
            self.preview.setFixedHeight(64)
            self.preview.setStyleSheet(
                f"color: {win.TEXT_MUTED}; background: {win.WINDOW_BG}; border-radius: 6px;"
            )
        layout.addWidget(self.preview)

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        self.caption = QLabel(caption)
        self.caption.setFont(_mono_font(11.0))
        self.caption.setStyleSheet(f"color: {win.TEXT_MUTED}; background: transparent;")
        row.addWidget(self.caption, 1)
        self.open_button = None
        if can_open:
            self.open_button = QPushButton("Open")
            self.open_button.setCursor(Qt.CursorShape.PointingHandCursor)
            self.open_button.setFont(_ui_font(11.5, 500))
            self.open_button.setToolTip("Open with the default app")
            self.open_button.setStyleSheet(
                f"QPushButton {{ background: transparent; border: none;"
                f" color: {win.TEXT_SECONDARY}; padding: 2px 6px; }}"
                f"QPushButton:hover {{ color: {win.TEXT_PRIMARY}; }}"
            )
            self.open_button.clicked.connect(self.open_clicked)
            row.addWidget(self.open_button)
        layout.addLayout(row)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self.rect().contains(
            event.position().toPoint()
        ):
            self.clicked.emit()
        super().mouseReleaseEvent(event)


class HistoryPanel(QWidget):
    """The panel itself. `show_beside_tray()` places and shows it; it closes
    on Escape, its close button, or when another window takes focus."""

    copy_requested = pyqtSignal(object)     # an output.CopiedItem, or a saved Path
    open_requested = pyqtSignal(object)     # a saved Path

    TABS = ("Copied", "Saved")

    def __init__(self, copied: list[output.CopiedItem], saved: list[Path]):
        super().__init__(None, Qt.WindowType.Tool
                         | Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.WindowStaysOnTopHint)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setWindowTitle("Snipux history")
        self.setFixedWidth(PANEL_W)
        win = tokens.Win
        self.setObjectName("panel")
        self.setStyleSheet(
            f"QWidget#panel {{ background: {win.WINDOW_BG};"
            f" border: 1px solid {win.BORDER}; }}" + scrollbar_style()
        )
        self._copied = copied
        self._saved = saved
        # Only close on losing focus once the panel has actually had it:
        # the deactivation that comes before the first activation is the
        # tray's own, not the user leaving.
        self._was_active = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(_PAD, _PAD, _PAD, _PAD)
        layout.setSpacing(12)

        header = QHBoxLayout()
        title = QLabel("History")
        title.setFont(_ui_font(14.0, 600))
        title.setStyleSheet(f"color: {win.TEXT_PRIMARY};")
        header.addWidget(title, 1)
        self.close_button = QPushButton()
        self.close_button.setIcon(design.icon("close", win.ICON_IDLE))
        self.close_button.setFixedSize(tokens.WinMetric.TITLEBAR_BTN, tokens.WinMetric.TITLEBAR_BTN)
        self.close_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.close_button.setToolTip("Close")
        self.close_button.setStyleSheet(
            f"QPushButton {{ background: transparent; border: none;"
            f" border-radius: {tokens.WinMetric.TITLEBAR_BTN_R}px; }}"
            f"QPushButton:hover {{ background: {win.CONTROL_BG_HOVER}; }}"
        )
        self.close_button.clicked.connect(self.close)
        header.addWidget(self.close_button)
        layout.addLayout(header)

        tabs = QHBoxLayout()
        tabs.setSpacing(4)
        self._tab_group = QButtonGroup(self)
        self._tab_group.setExclusive(True)
        self.tab_buttons: dict[str, QPushButton] = {}
        for name in self.TABS:
            button = QPushButton(name)
            button.setCheckable(True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setFont(_ui_font(12.5, 500))
            button.setFixedHeight(30)
            button.setStyleSheet(
                f"QPushButton {{ background: {win.CHROME_BG}; color: {win.TEXT_MUTED};"
                f" border: 1px solid {win.SEGMENT_BORDER}; border-radius: 8px; }}"
                f"QPushButton:hover {{ color: {win.TEXT_SECONDARY}; }}"
                f"QPushButton:checked {{ background: {win.SELECTED_BG};"
                f" color: {win.TEXT_PRIMARY}; }}"
            )
            self._tab_group.addButton(button)
            self.tab_buttons[name] = button
            tabs.addWidget(button)
            button.clicked.connect(lambda checked=False, n=name: self.show_tab(n))
        layout.addLayout(tabs)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setStyleSheet("QScrollArea { background: transparent; }")
        layout.addWidget(self._scroll, 1)

        self.current_tab = self.TABS[0]
        self.cards: list[_Card] = []
        self.show_tab(self.TABS[0])

    def refresh(self, copied: list[output.CopiedItem], saved: list[Path]) -> None:
        self._copied = copied
        self._saved = saved
        self.show_tab(self.current_tab)

    def show_tab(self, name: str) -> None:
        self.current_tab = name
        self.tab_buttons[name].setChecked(True)
        body = QWidget()
        body.setStyleSheet("background: transparent;")
        column = QVBoxLayout(body)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(_CARD_GAP)
        self.cards = []
        if name == "Copied":
            for item in self._copied:
                card = self._copied_card(item)
                card.clicked.connect(lambda i=item: self.copy_requested.emit(i))
                self.cards.append(card)
        else:
            for path in self._saved:
                card = self._saved_card(path)
                card.clicked.connect(lambda p=path: self.copy_requested.emit(p))
                card.open_clicked.connect(lambda p=path: self.open_requested.emit(p))
                self.cards.append(card)
        for card in self.cards:
            column.addWidget(card)
        if not self.cards:
            column.addWidget(self._empty_note(name))
        column.addStretch(1)
        self._scroll.setWidget(body)

    def _copied_card(self, item: output.CopiedItem) -> _Card:
        when = _when(item.copied_at)
        if item.kind == "image":
            return _Card(f"{item.value.width()} × {item.value.height()}  ·  {when}",
                         image=item.value)
        if item.kind == "text":
            return _Card(f"Text  ·  {when}", text=item.value)
        return _Card(f"Recording  ·  {when}", placeholder=item.value.name)

    def _saved_card(self, path: Path) -> _Card:
        try:
            when = _when(datetime.datetime.fromtimestamp(path.stat().st_mtime))
        except OSError:
            when = ""
        thumb = saved_thumbnail(path)
        if thumb is not None:
            return _Card(when, image=thumb, can_open=True)
        return _Card(f"Recording  ·  {when}", placeholder=path.name, can_open=True)

    def _empty_note(self, name: str) -> QLabel:
        if name == "Copied":
            text = ("Nothing copied yet. What you copy with Snipux shows up "
                    "here until Snipux quits.")
        else:
            text = "Nothing saved yet."
        note = QLabel(text)
        note.setWordWrap(True)
        note.setFont(_ui_font(12.5, 400))
        note.setStyleSheet(f"color: {tokens.Win.TEXT_NOTE};")
        return note

    def show_beside_tray(self) -> None:
        """Down the right of the screen the pointer is on (where the tray
        was just clicked), inside the work area so the taskbar stays clear.
        Logical, virtual-desktop coordinates throughout: `availableGeometry`
        and `setGeometry` are both in that space."""
        screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
        if screen is not None:
            area = screen.availableGeometry()
            self.setGeometry(QRect(
                area.right() + 1 - PANEL_W - _EDGE_GAP,
                area.top() + _EDGE_GAP,
                PANEL_W,
                area.height() - 2 * _EDGE_GAP,
            ))
        self.show()
        self.raise_()
        self.activateWindow()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.close()
            return
        super().keyPressEvent(event)

    def changeEvent(self, event) -> None:
        if event.type() == event.Type.ActivationChange:
            if self.isActiveWindow():
                self._was_active = True
            elif self._was_active:
                self.close()
        super().changeEvent(event)
