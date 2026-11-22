"""The overlay's small pieces of chrome: the top hint HUD, the toast, the
close button and the delay countdown.

Four widgets that each say one thing and take no part in selecting or
drawing: `HintHUD` lists the keys, `Toast` confirms what just happened,
`_CloseButton` is the way out before anything is selected, and
`DelayCountdown` counts a delayed capture down. `OverlayWindow` builds,
places and shows them; `review.py` uses the same `Toast`.

Separate from `overlay.py` because each is self-contained -- it is handed
text or a number and paints it -- so none of them needs the window's state
to be read or changed. Nothing in this module imports `overlay.py`.
"""

from __future__ import annotations

from PyQt6.QtCore import QRect, QRectF, QSize, Qt, QTimer
from PyQt6.QtGui import QColor, QFont, QPainter
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QWidget

from snipux import design
from snipux.toolbar import _Chrome, _bar_keys


# ---------------------------------------------------------------------------
# Top hint HUD (SNX-46)
# ---------------------------------------------------------------------------
# docs/design/overlay-redesign.md's "Top hint HUD" section is the authority
# here. It's a first-run affordance, behind a preference (`hints`) --
# see `OverlayWindow.set_hints_enabled` -- and the spec's own "Re-framing"
# section already reserves the room for it: `OverlayWindow._TOP_CLEARANCE`
# (52) is 8px clear of `tokens.Metric.HUD_H` (44), so the two have agreed
# since SNX-33 landed and this ticket only has to paint into the space
# already held open.
#
# SNX-65: the preference now defaults *off*. Read as a banner across the
# whole top of every capture rather than help, it was the first thing the
# eye landed on and competed with the snip it sits above -- exactly the
# "first-run affordance worth hiding after N successful captures" this
# section already called it out as. `_resize_selection` stops reserving
# `_TOP_CLEARANCE` the moment hints are off, so the selection gets the room
# back rather than keeping a dead strip held open for a bar nobody is
# shown; press `?` to bring it up for the session (`keyPressEvent` below).


class HintHUD(_Chrome):
    """The overlay's full-width top hint bar, per docs/design/overlay-
    redesign.md's "Top hint HUD" section: `Esc discard ink · Enter copy &
    dismiss · P H R O L A S T B E pick a tool · drag any edge to re-frame ·
    arrows nudge · Alt+arrows resize -- the ink stays where you put it`. The
    last two segments are this file's own addition (#88) -- the spec
    predates the feature and never named it.

    A real child widget of `OverlayWindow` -- built from real `QLabel`
    segments, not painted inside `OverlayWindow.paintEvent` -- the same
    reasoning `Toast` documents for itself: `OverlayWindow.rendered_image`
    flattens `_marks` onto the frame via `shapes.render_selection` and never
    touches this window's chrome at all, so the HUD can't leak into an
    export regardless of whether it happens to be on screen at the moment
    `copy`/`save` is called.

    Key names (`Esc`, `Enter`, the eight tool shortcuts) are their own
    labels in the mono family at `HUD_KEY` (pure white); the surrounding
    prose is the UI family at the muted `HUD_TEXT` -- per the spec's "Key
    names are mono in pure white," so a key reads as a key at a glance
    rather than blending into the sentence around it.
    """

    # (text, is_key) in reading order. The key segment for the tool
    # shortcuts is built from the bar's own slots (`_bar_keys`) rather than
    # typed out -- if a shortcut or the slot order ever changes, this line
    # follows it instead of silently drifting out of sync the way a
    # hand-typed copy of the same letters could.
    def _segments(self) -> list[tuple[str, bool]]:
        keys = " ".join(_bar_keys())
        return [
            ("Esc", True),
            (" discard ink · ", False),
            ("Enter", True),
            (" copy & dismiss · ", False),
            (keys, True),
            (" pick a tool · drag any edge to re-frame · ", False),
            ("arrows", True),
            (" nudge · ", False),
            ("Alt+arrows", True),
            (" resize — the ink stays where you put it", False),
        ]

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setFixedHeight(design.tokens.Metric.HUD_H)

        # Full-width and centred, per "Full width... contents centred" --
        # a stretch on both sides of the segment labels is what centres a
        # variable-width run of text without this widget needing to measure
        # it itself the way OverlayWindow's own chips do.
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addStretch(1)
        for text, is_key in self._segments():
            layout.addWidget(self._segment_label(text, is_key))
        layout.addStretch(1)

    def _segment_label(self, text: str, is_key: bool) -> QLabel:
        label = QLabel(text, self)
        label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        family = design.font_families().mono if is_key else design.font_families().ui
        font = QFont(family)
        size, weight = design.tokens.Font.HUD
        font.setPixelSize(round(size))
        font.setWeight(QFont.Weight(weight))
        label.setFont(font)
        colour = design.color("HUD_KEY" if is_key else "HUD_TEXT")
        label.setStyleSheet(f"color: {colour.name()};")
        return label

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        # A flat, translucent fill with no glass under it. The redesign's
        # spec gives the HUD a 3px backdrop blur, but the glass (#70) is the
        # bars handoff's, for its bars and menus; this banner predates it
        # and is off unless asked for.
        painter.fillRect(QRectF(self.rect()), design.color("HUD_BG"))
        painter.end()


# ---------------------------------------------------------------------------
# Toast (SNX-45)
# ---------------------------------------------------------------------------


class Toast(_Chrome):
    """The overlay redesign's toast: bottom centre, above everything, per
    docs/design/overlay-redesign.md's "Toast" section.

    A real child widget of `OverlayWindow`, built and positioned the same
    way `FloatingBar`/`StylePopover`/`CaptureModePopover` are -- never
    painted inside `OverlayWindow.paintEvent`, which is what the spec means
    by "chrome painted over the overlay rather than something drawn into
    the frame": `OverlayWindow.rendered_image` flattens `_marks` onto the
    frame's own pixmap via `shapes.render_selection` and never touches this
    widget (or any other chrome) at all, so a toast can never end up in an
    export regardless of whether it happens to be on screen at the moment
    `copy`/`save` is called.

    There is only ever one toast, per the spec's "a new toast replaces the
    old one and restarts the timer" -- `show_message` overwrites whatever
    the previous call was showing and restarts `_timer` rather than a
    caller stacking a second widget, since this class is itself the single
    instance `OverlayWindow` keeps as `_toast`.
    """

    _ICON_SIZE = 15
    # Prose-only spacing between the glyph and the message, like
    # `_ToolPill`/`_FROZEN_INNER_GAP`'s own un-tokenized gaps elsewhere in
    # this file.
    _GAP = 8

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        metric = design.tokens.Metric

        layout = QHBoxLayout(self)
        layout.setContentsMargins(
            metric.TOAST_PAD_H, metric.TOAST_PAD_V, metric.TOAST_PAD_H, metric.TOAST_PAD_V
        )
        layout.setSpacing(self._GAP)

        self._icon_label = QLabel(self)
        self._icon_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        layout.addWidget(self._icon_label)

        self._text_label = QLabel(self)
        self._text_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        font = QFont(design.font_families().ui)
        size, weight = design.tokens.Font.TOAST
        font.setPixelSize(round(size))
        font.setWeight(QFont.Weight(weight))
        self._text_label.setFont(font)
        self._text_label.setStyleSheet(f"color: {design.color('TOAST_FG').name()};")
        layout.addWidget(self._text_label)

        # Single-shot, restarted (not re-created) by every show_message
        # call -- QTimer.start() on an already-running timer resets its
        # remaining time, which is exactly the spec's "restarts the timer"
        # rather than letting an earlier call's dismissal fire early.
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(metric.TOAST_MS)
        self._timer.timeout.connect(self.hide)

        self.hide()

    def show_message(self, icon_name: str, text: str, bounds: QRectF) -> None:
        """Show `text` next to `icon_name`'s glyph, positioned at the
        bottom centre of `bounds`, and (re)start the `TOAST_MS`
        auto-dismiss timer.

        Updates this same widget's content rather than creating a new one
        -- there is only ever one toast on screen, per the class docstring
        -- so a second call while the first is still showing both replaces
        the message and restarts the timer in one step.

        `bounds` is the selection's own monitor rather than the parent's
        size, for the reason `FloatingBar.reposition` sets out at length:
        "the bottom centre of the window" is the bottom centre of the whole
        virtual desktop once one window spans every monitor, which is both
        the wrong monitor to confirm a snip on and, where monitor heights
        differ, potentially a gap that displays nothing at all.
        """
        metric = design.tokens.Metric
        pixmap = design.icon(icon_name, design.color("TOAST_FG")).pixmap(
            self._ICON_SIZE, self._ICON_SIZE
        )
        self._icon_label.setPixmap(pixmap)
        self._text_label.setText(text)

        size = self.sizeHint()
        left = bounds.left() + (bounds.width() - size.width()) / 2
        top = bounds.bottom() - metric.TOAST_BOTTOM - size.height()
        self.setGeometry(round(left), round(top), size.width(), size.height())

        self.show()
        self.raise_()  # "above everything" -- including the bar and trays
        self._timer.start()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        metric = design.tokens.Metric

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(design.color("TOAST_BG"))
        painter.drawRoundedRect(QRectF(self.rect()), metric.TOAST_RADIUS, metric.TOAST_RADIUS)
        painter.end()


# ---------------------------------------------------------------------------
# Close button (SNX-80)
# ---------------------------------------------------------------------------
# `_bar` and the per-selection chips (the dimension chip, the frozen pill)
# all only ever exist once a selection does -- `_sync_bar_visibility`,
# `OverlayWindow.paintEvent`'s own `if self._selection is not None` guard --
# but a user who opens the overlay and decides, before dragging anything at
# all, that they don't want to snip anything has exactly as much right to a
# visible way out as one who's already mid-annotation. SNX-65 turned off the
# only thing (the hint HUD) that ever told anyone Escape was that way out,
# so this is its own small piece of chrome, independent of both: shown for
# as long as `OverlayWindow` itself is (`showEvent`/`hideEvent`, the same
# pairing `_toast`/`_hud` already use), never gated on `_selection`,
# `_bar.active_tool` or `_marks` the way everything else in this file is.


class _CloseButton(QPushButton):
    """A small round button pinned to a fixed corner of the overlay: the
    one control guaranteed to be on screen, and clickable, from the moment
    the overlay opens to the moment it closes.

    Unlike `_IconButton` (idle-transparent, relying on the bar's own glass
    fill for contrast against the frozen desktop underneath), this button
    has no bar behind it -- it sits directly over whatever pixels happen to
    be there, which could be any colour at all -- so its background is
    always painted, not just on hover, using the same `BAR_BG`/
    `BAR_BG_ALPHA` glass the floating bar itself uses. A plain QSS
    `:hover` rule is enough for the hover state (no custom paintEvent
    needed, unlike `_SwatchButton`'s two-colour ring): there is only ever
    one fill to swap.
    """

    _SIZE = design.tokens.Metric.BTN
    _ICON_SIZE = design.tokens.Metric.ICON
    # How much brighter the fill reads on hover -- enough to register as a
    # state change without a dedicated token for a control this small, the
    # same un-tokenized-literal convention `OverlayWindow`'s own
    # `_CORNER_BRACKET_OFFSET` and friends already follow.
    _HOVER_ALPHA_BOOST = 0.07

    def __init__(self, parent=None):
        super().__init__(parent)
        # No keyboard focus, for this and every other button over the
        # overlay: its keys -- Enter, Space, the tool letters -- are the
        # overlay's own (`keyPressEvent`). A push button that can take
        # focus is handed it as the window opens, and then Enter and Space
        # press the button instead. Measured on GNOME 46 Wayland: this
        # button held focus, so Enter closed the snip rather than copying.
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setFixedSize(self._SIZE, self._SIZE)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        # Names Escape by its full word, not the "Esc" abbreviation every
        # other tooltip in this file uses for a shortcut -- this is the
        # one place a user with the hint HUD off (SNX-65's new default)
        # learns the keyboard route exists at all, so it spells the key
        # out rather than assuming the abbreviation is already familiar.
        self.setToolTip("Close — Escape")
        self.setFlat(True)

        bg = design.color("BAR_BG")
        hover_bg = QColor(bg)
        hover_bg.setAlphaF(min(1.0, bg.alphaF() + self._HOVER_ALPHA_BOOST))
        self.setStyleSheet(
            "QPushButton { border: none; border-radius: %dpx;"
            " background: rgba(%d, %d, %d, %s); }"
            "QPushButton:hover { background: rgba(%d, %d, %d, %s); }"
            % (
                self._SIZE // 2,
                bg.red(), bg.green(), bg.blue(), bg.alphaF(),
                hover_bg.red(), hover_bg.green(), hover_bg.blue(), hover_bg.alphaF(),
            )
        )
        self.setIcon(design.icon("close", design.color("TEXT_PRIMARY")))
        self.setIconSize(QSize(self._ICON_SIZE, self._ICON_SIZE))


# ---------------------------------------------------------------------------
# Delay countdown (SNX-50)
# ---------------------------------------------------------------------------
# docs/design/overlay-redesign.md's "Capture modes" entry for Delay is the
# authority: "the overlay dismisses, waits, re-grabs and re-opens. Show a
# countdown." The prototype never simulates Delay (Region is the only mode it
# simulates, per that same section's opening line), so there is no reference
# markup for the countdown's own look -- styled here from the same BAR_BG
# glass and TEXT_PRIMARY/mono readout the rest of this file's chrome already
# uses, rather than inventing a one-off palette for a single widget.


class DelayCountdown(QWidget):
    """A small, top-level countdown shown while `OverlayWindow` is hidden
    for a delayed re-capture (`OverlayWindow._start_delayed_capture`).

    Deliberately *not* a child of `OverlayWindow`, unlike every other piece
    of chrome in this file: the entire reason the overlay hides for the
    delay is so it isn't in its own screenshot (per the spec: "the overlay
    dismisses... so it is not in its own screenshot" is the point of Delay
    in the first place), and a child widget goes invisible the instant its
    parent does -- see `OverlayWindow.hideEvent`, which relies on exactly
    that to take `_bar`/`_style_popover`/`_popover`/`_toast`/`_hud` down with it. A
    countdown built the same way would vanish along with the window it is
    supposed to be standing in for, which is the one thing it must not do.
    """

    _SIZE = 96
    _BG_ALPHA = 0.72  # the same "glass over the desktop" treatment as the bar

    def __init__(self):
        # No parent, ever -- see the class docstring.
        super().__init__(None)
        # Frameless/always-on-top so it reads as a HUD rather than a window
        # a user could accidentally click into and lose focus of, as
        # `OverlayWindow`'s own `setWindowFlags` call does.
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setFixedSize(self._SIZE, self._SIZE)

        self._label = QLabel(self)
        self._label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._label.setGeometry(0, 0, self._SIZE, self._SIZE)
        font = QFont(design.font_families().mono)
        font.setPixelSize(36)
        font.setWeight(QFont.Weight(600))
        self._label.setFont(font)
        self._label.setStyleSheet(f"color: {design.color('TEXT_PRIMARY').name()};")

    def set_seconds_remaining(self, seconds: int) -> None:
        self._label.setText(str(seconds))

    def show_centered_on(self, geometry: QRect) -> None:
        """Position centred over `geometry` -- the monitor the hidden
        `OverlayWindow` was being worked on, in absolute coordinates -- and
        show.
        """
        center = QRectF(geometry).center()
        self.move(round(center.x() - self._SIZE / 2), round(center.y() - self._SIZE / 2))
        self.show()
        self.raise_()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        bg = QColor(design.tokens.Color.BAR_BG)
        bg.setAlphaF(self._BG_ALPHA)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(bg)
        painter.drawEllipse(QRectF(self.rect()))
        painter.end()
