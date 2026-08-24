"""snipux overlay — design tokens.

Single source of truth for the in-place capture overlay. Every literal in the
design reference (reference/Snipux Overlay.dc.html) resolves to a value here.
Import these rather than re-typing hex codes in widget code.
"""

# ---------------------------------------------------------------- colour
class Color:
    # Overlay chrome (floating bar, tray, popover) — glass over a frozen desktop.
    BAR_BG          = "#1a1c18"   # painted at 93% alpha
    BAR_BG_ALPHA    = 0.93
    BAR_BORDER      = "#ffffff"   # at 10% alpha
    BAR_BORDER_ALPHA = 0.10
    DIVIDER         = "#ffffff"   # at 12% alpha
    DIVIDER_ALPHA   = 0.12

    # Icon buttons
    ICON_IDLE       = "#a8afa0"
    ICON_ACTIVE     = "#f8faf0"
    ICON_HOVER_BG   = "#ffffff"   # at 9% alpha
    ICON_ACTIVE_BG  = "#ffffff"   # at 16% alpha
    ICON_DISABLED   = "#5d6157"
    ICON_NEUTRAL    = "#d7dacb"   # copy button, always enabled
    DANGER_BG       = "#c85050"   # at 22% alpha, clear-ink hover
    DANGER_FG       = "#f5a3a3"

    # Text on chrome
    TEXT_PRIMARY    = "#f1f3e8"
    TEXT_MUTED      = "#8f9689"
    TEXT_READOUT    = "#c6cab8"   # monospace numerals

    # Primary accent — the capture-mode chip and the default ink
    ACCENT          = "#e3ff4f"
    ACCENT_FG       = "#15170e"   # text on accent

    # Selection frame
    SEL_STROKE      = "#ffffff"   # at 92% alpha
    SEL_STROKE_ALPHA = 0.92
    SEL_ANTS        = "#1b1c16"   # dark dashes over the white stroke
    HANDLE          = "#ffffff"
    DIM             = "#0c0d0a"   # scrim outside the selection, 62% alpha
    DIM_ALPHA       = 0.62

    # Chips floating above the selection
    CHIP_LIGHT_BG   = "#e9ecf2"
    CHIP_LIGHT_FG   = "#12141a"
    CHIP_LIGHT_MUTE = "#4b5563"
    CHIP_DARK_BG    = "#141512"   # at 78% alpha
    CHIP_DARK_FG    = "#e5e7d9"

    # Toast
    TOAST_BG        = "#e9ecf2"   # at 96% alpha
    TOAST_FG        = "#12141a"

    # Step badge ring (annotation tool, not chrome)
    STEP_RING       = "#ffffff"   # opaque -- unlike the chrome whites above

    # Text label chip (annotation tool, not chrome)
    TEXT_LABEL_BG        = "#0c0e12"   # at 72% alpha
    TEXT_LABEL_BG_ALPHA  = 0.72
    TEXT_LABEL_RING      = "#ffffff"   # at 16% alpha
    TEXT_LABEL_RING_ALPHA = 0.16

# Ink swatches, in bar order. First entry is the default.
INK_SWATCHES = [
    ("Acid",    "#e3ff4f"),
    ("Red",     "#ef4444"),
    ("Sky",     "#38bdf8"),
    ("Emerald", "#10b981"),
    ("Violet",  "#a855f7"),
    ("White",   "#ffffff"),
    ("Ink",     "#12141a"),
]

# ---------------------------------------------------------------- type
class Font:
    UI    = "IBM Plex Sans"       # all chrome labels
    MONO  = "IBM Plex Mono"       # every numeral, dimension and hex readout

    # (px, weight) — px are logical pixels at 1x
    CHIP_LABEL    = (12.5, 600)   # "Region", "Save"
    TRAY_LABEL    = (12.0, 500)   # "Pen" in the settings tray
    TRAY_HINT     = (11.5, 400)   # "Drag to draw freehand"
    READOUT       = (11.0, 500)   # "5px", mono
    DIM_CHIP      = (12.0, 600)   # "1040 × 560", mono
    DIM_CHIP_MUTE = (12.0, 400)   # "2 marks", mono
    HUD           = (12.0, 400)   # top hint bar
    MENU_LABEL    = (12.5, 500)
    MENU_NOTE     = (11.0, 400)
    TOAST         = (12.5, 500)
    STEP_BADGE    = (13.0, 600)

# ---------------------------------------------------------------- metrics
class Metric:
    # Floating bar
    BAR_RADIUS       = 14
    BAR_PAD_V        = 7
    BAR_PAD_H        = 8
    BAR_GAP          = 3          # between icon buttons
    BAR_DIVIDER_GAP  = 6          # extra margin either side of a divider
    DIVIDER_H        = 22
    BAR_OFFSET_Y     = 18         # gap between selection bottom and bar top
    BAR_MIN_EDGE     = 400        # bar centre is clamped this far from a screen edge

    # Buttons
    BTN              = 34         # square icon button
    BTN_RADIUS       = 10
    ICON             = 18         # glyph box inside a 34px button
    ICON_STROKE      = 1.55
    CHIP_H           = 34
    CHIP_PAD_L       = 12
    CHIP_PAD_R       = 9

    # Settings tray (appears only while a drawing tool is held)
    TRAY_RADIUS      = 12
    TRAY_PAD_V       = 8
    TRAY_PAD_H       = 12
    TRAY_GAP         = 12
    TRAY_OFFSET_Y    = 8          # gap between bar and tray
    SWATCH           = 22
    SWATCH_RADIUS    = 7
    SWATCH_GAP       = 6
    SLIDER_W         = 104
    SLIDER_TRACK_H   = 4
    SLIDER_THUMB     = 13

    # Capture-mode popover
    MENU_W           = 262
    MENU_PAD         = 6
    MENU_RADIUS      = 12
    MENU_ROW_PAD_V   = 8
    MENU_ROW_PAD_H   = 9
    MENU_ROW_RADIUS  = 8
    MENU_OFFSET      = 8          # opens UPWARD from the bar; see README

    # Selection
    # The reference prototype uses 200x140; SNX-33 deliberately shrinks
    # this floor to 16x16 so a taskbar icon or a single line of text can
    # still be snipped.
    SEL_MIN_W        = 16
    SEL_MIN_H        = 16
    SEL_STROKE_W     = 1
    ANTS_DASH        = (7, 7)     # on, off
    ANTS_PERIOD_MS   = 700        # one 14px dash cycle
    CORNER_LEN       = 26         # L-bracket arm length
    CORNER_W         = 4
    HANDLE_LONG      = 34         # edge handle: 34 × 9
    HANDLE_SHORT     = 9
    HANDLE_HIT       = 20         # invisible corner hit target
    CHIP_OFFSET_Y    = 38         # dimension chip sits this far above the selection

    # Annotation defaults
    STEP_D           = 26
    STEP_RING_W      = 2
    TEXT_LABEL_RADIUS = 5
    TEXT_LABEL_PAD_V = 3
    TEXT_LABEL_PAD_H = 8
    TEXT_LABEL_RING_W = 1
    HIGHLIGHT_MULT   = 3.5        # stroke width multiplier for the highlighter
    HIGHLIGHT_ALPHA  = 0.34
    STROKE_MIN       = 1
    STROKE_MAX       = 26
    STROKE_DEFAULT   = 5
    BLUR_MIN         = 2
    BLUR_MAX         = 20
    BLUR_DEFAULT     = 8

    # Top hint HUD
    HUD_H            = 44

    TOAST_RADIUS     = 11
    TOAST_PAD_V      = 10
    TOAST_PAD_H      = 15
    TOAST_BOTTOM     = 34
    TOAST_MS         = 2000

# ---------------------------------------------------------------- shadow
# Qt has no CSS box-shadow. Use QGraphicsDropShadowEffect on the frameless
# popup widgets, or paint a blurred rounded rect behind them.
class Shadow:
    BAR   = dict(blur=50, dy=24, color="#000000", alpha=0.62)
    TRAY  = dict(blur=40, dy=18, color="#000000", alpha=0.55)
    MENU  = dict(blur=60, dy=26, color="#000000", alpha=0.72)
    CHIP  = dict(blur=22, dy=8,  color="#000000", alpha=0.45)
    TOAST = dict(blur=44, dy=20, color="#000000", alpha=0.55)

# ---------------------------------------------------------------- behaviour
TOOLS = ["pen", "highlighter", "arrow", "rect", "step", "text", "blur", "eraser"]

# Tools whose settings tray is the colour + stroke tray.
DRAW_TOOLS = ["pen", "highlighter", "arrow", "rect", "step", "text"]

SHORTCUTS = {
    "P": "pen", "H": "highlighter", "A": "arrow", "R": "rect",
    "S": "step", "T": "text", "B": "blur", "E": "eraser",
}

CAPTURE_MODES = [
    ("Region",      "crop",    "Drag any rectangle"),
    ("Window",      "window",  "Snap to a window"),
    ("Full screen", "monitor", "Whole display"),
    ("Freeform",    "pen",     "Lasso an odd shape"),
]

DELAYS = ["Off", "3s", "5s", "10s"]

TOOL_HINTS = {
    "pen":         "Drag to draw freehand",
    "highlighter": "Sweep over the line that matters",
    "arrow":       "Drag from tail to head",
    "rect":        "Drag to box something in",
    "step":        "Click to drop the next number",
    "text":        "Click, then type into the label",
    "blur":        "Drag over anything private",
    "eraser":      "Click a mark to remove it",
}
