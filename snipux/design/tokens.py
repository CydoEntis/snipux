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
    ICON_HOVER_BG_ALPHA = 0.09
    ICON_ACTIVE_BG  = "#ffffff"   # at 16% alpha
    ICON_ACTIVE_BG_ALPHA = 0.16
    ICON_DISABLED   = "#5d6157"
    ICON_NEUTRAL    = "#d7dacb"   # copy button, always enabled
    DANGER_BG       = "#c85050"   # at 22% alpha, clear-ink hover
    DANGER_BG_ALPHA = 0.22
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
    CHIP_DOT        = "#9ca3af"   # the middot between size and mark count
    CHIP_DARK_BG    = "#141512"
    CHIP_DARK_BG_ALPHA = 0.78
    CHIP_DARK_FG    = "#e5e7d9"

    # Top hint HUD
    HUD_BG          = "#141512"   # at 50% alpha
    HUD_BG_ALPHA    = 0.50
    HUD_TEXT        = "#d9dbcd"   # surrounding prose
    HUD_KEY         = "#ffffff"   # key names -- pure white, mono

    # Toast
    TOAST_BG        = "#e9ecf2"   # at 96% alpha
    TOAST_BG_ALPHA  = 0.96
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
    FROZEN        = (12.0, 400)   # "Frozen" pill label, sans
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

# SNX-64: shapes.py has always fully implemented Ellipse, Line and Crop, but
# nothing reachable from the redesigned chrome ever named them, so the bar
# only ever offered eight of the eleven tools the owner asked -- before the
# redesign started -- to keep, because he had tried all eleven and they
# worked. That instruction outranks the design handoff's own "eight tools,
# no more bar buttons" rule, the same way the 16x16 minimum selection
# (TODO.md) already outranks it: this is the second deliberate deviation.
# The handoff's own guidance for a tool that doesn't fit the eight is a
# submenu off an existing button rather than a bar button of its own, so
# these three are reachable only through the rect button's own popover
# (`ShapeToolPopover` in overlay.py) -- `rect` itself is first so the
# popover's default selection matches the button's own glyph.
RECT_GROUP = ["rect", "ellipse", "line", "crop"]

# Tools whose settings tray is the colour + stroke tray. Ellipse/Line/Crop
# take their ink colour and stroke width from it exactly the way Rectangle
# already does -- see RECT_GROUP above.
DRAW_TOOLS = ["pen", "highlighter", "arrow", "rect", "step", "text", "ellipse", "line", "crop"]

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

# ---------------------------------------------------------------------------
# Settings and review window chrome (design_handoff_snipux)
# ---------------------------------------------------------------------------
# The overlay's own palette above is warm glass over a frozen desktop. These
# two are ordinary windows and use an opaque neutral dark instead -- see
# docs/design/handoff-windows.md, which is the authority for everything in
# this section.

class Win:
    """Settings and review window chrome. Opaque — no alpha compositing."""
    # Surfaces, back to front
    WINDOW_BG       = "#14161a"   # window body / content pane
    CHROME_BG       = "#191c21"   # title bar, nav rail, footer, inset fields
    BORDER          = "#2a2e36"   # window outline
    SEPARATOR       = "#23262d"   # title-bar and footer rules
    HAIRLINE        = "#22252c"   # rules inside a content pane
    RADIUS_NOTE     = "12px window, 9px control, 8px inset row"

    # Workspace behind the reviewed image (a radial, see Gradient.WORKSPACE)
    IMAGE_BORDER    = "#454b56"   # 1px edge on the screenshot itself

    # Controls
    CONTROL_BG      = "#1c1f25"   # secondary button fill
    CONTROL_BG_HOVER = "#252931"
    CONTROL_BORDER  = "#2f333b"
    CONTROL_BORDER_HOVER = "#3a3f49"
    FIELD_BG        = "#191c21"   # text input, inset well
    FIELD_BORDER    = "#2b2f36"
    SEGMENT_BORDER  = "#262a31"   # segmented-control well
    SELECTED_BG     = "#2c313c"   # active nav row, active segment
    ROW_HOVER       = "#20242b"
    TOGGLE_OFF      = "#33383f"   # switch track, off
    TOGGLE_KNOB     = "#f1f3e8"

    # Text
    TEXT_PRIMARY    = "#e7eaf1"
    TEXT_BODY       = "#d3d8e1"
    TEXT_TITLE      = "#d6dae2"   # title-bar label
    TEXT_SECONDARY  = "#c3c9d4"
    TEXT_MUTED      = "#8a92a1"
    TEXT_NOTE       = "#79808f"   # sub-label under a control
    TEXT_FAINT      = "#6d7484"
    TEXT_SECTION    = "#616876"   # uppercase group heading
    TEXT_DISABLED   = "#5f6674"
    ICON_IDLE       = "#8d94a3"
    ICON_ACTIVE     = "#f3f5f9"
    TITLEBAR_ICON   = "#7c8494"
    CLOSE_HOVER     = "#c0392b"   # GNOME-ish red, white glyph

    # Status semantics — used by the conflict check and the saved/dirty line
    OK_FG           = "#a8c86a"
    OK_BG           = "#a0c85a"   # at 10% alpha
    OK_BG_ALPHA     = 0.10
    OK_BORDER       = "#a0c85a"   # at 24% alpha
    OK_BORDER_ALPHA = 0.24
    OK_STRONG       = "#9ec46a"   # "Saved" tick
    WARN_FG         = "#c8a54a"   # "Unsaved changes", "Edited — not saved"
    ERR_FG          = "#e8a5a5"
    ERR_BG          = "#c85050"   # at 12% alpha
    ERR_BG_ALPHA    = 0.12
    ERR_BORDER      = "#c85050"   # at 28% alpha
    ERR_BORDER_ALPHA = 0.28
    PATH_FG         = "#c8d96a"   # filename preview, mono


class Gradient:
    # The review window's canvas behind the screenshot. Qt: QRadialGradient,
    # centred at 50% / 30% of the viewport, radius ~0.85 × width.
    WORKSPACE = ("radial", (0.50, 0.30), 0.85, [(0.0, "#171a1f"), (1.0, "#0c0d10")])


class WinMetric:
    """Settings + review window geometry. Logical pixels."""
    SETTINGS_W       = 780
    SETTINGS_H       = 580
    REVIEW_W         = 1020
    REVIEW_H         = 700

    WINDOW_RADIUS    = 12
    TITLEBAR_H       = 42
    TITLEBAR_BTN     = 26
    TITLEBAR_BTN_R   = 6
    TITLEBAR_ICON    = 14

    NAV_W            = 182


# Settings nav rail
    NAV_PAD          = (12, 10)   # v, h
    NAV_ROW_PAD      = (9, 10)
    NAV_ROW_RADIUS   = 8
    NAV_ROW_GAP      = 2
    NAV_ICON         = 16

    PANE_PAD         = (20, 22)   # content pane padding, v/h
    GROUP_GAP        = 22         # between labelled groups
    FIELD_GAP        = 11         # within a group

    CONTROL_H        = 36         # text field, secondary button
    RECORDER_H       = 38         # the shortcut field is one step taller
    CONTROL_RADIUS   = 9
    FOOTER_H         = 56
    FOOTER_BTN_H     = 34

    SWITCH_W         = 34         # toggle track
    SWITCH_H         = 19
    SWITCH_KNOB      = 15
    SWITCH_PAD       = 2
    RADIO_D          = 15         # radio-card ring
    RADIO_DOT        = 7
    CARD_PAD         = (11, 12)
    SETTINGS_SWATCH  = 30         # larger than the overlay's 22px tray swatch

    # Review window
    REVIEW_IMG_BORDER = 1
    REVIEW_IMG_RING   = 7        # rgba(255,255,255,.02) outer ring
    REVIEW_BADGE_INSET = (16, 14) # h, v from the canvas corner
    REVIEW_BAR_BOTTOM = 18        # floating bar above the canvas floor
    REVIEW_FOOTER_PAD = (13, 16)
    ZOOM_STEPS        = (60, 160, 20)  # min, max, step


# Settings nav rail, in order: (id, icon, label)
SETTINGS_NAV = [
    ("capture", "camera", "Capture"),
    ("saving",  "save",   "Saving"),
    ("ink",     "pen",    "Annotation"),
    ("tray",    "panel",  "Tray & startup"),
]

# "After capture" — mutually exclusive, radio cards. (id, label, note)
# What happens once the selection is made. One axis, three answers, in
# order of how much of your attention each one asks for.
#
# It used to be a destination -- review / clip / file -- but two of those
# three were the same behaviour under different names: `clip` and `file`
# both meant "annotate in place and press a button when you're done", and
# nothing anywhere read which of the two it was. What a user actually picks
# between is where the editing happens, so that is what this asks.
AFTER_CAPTURE = [
    ("instant", "Capture and finish",
     "Straight to the clipboard the moment the selection is made -- no "
     "overlay, no toolbar, nothing to dismiss."),
    ("edit", "Capture and annotate",
     "The frozen frame stays up with the tools on it. Copy or save when "
     "you are done."),
    ("review", "Capture and review",
     "Opens the review window afterwards, which annotates too."),
]

# The one an upgrading user gets, and what `Chooser` starts on before
# Settings seeds it. Not `AFTER_CAPTURE[0]`: the list is ordered for the
# Settings pane to read down, and the default is a separate decision --
# annotating in place is what every version before this did.
AFTER_DEFAULT = "edit"

# Filename pattern tokens offered as clickable chips under the field.
FILENAME_TOKENS = [
    ("%Y", "Year"), ("%m", "Month"), ("%d", "Day"),
    ("%H", "Hour"), ("%M", "Minute"), ("%S", "Second"),
    ("%c", "Counter"), ("%w", "Active window"),
]
FILENAME_DEFAULT = "Screenshot from %Y-%m-%d %H-%M-%S"

FORMATS = ["PNG", "JPEG", "WebP"]      # quality slider shows for the lossy two
QUALITY_DEFAULT = 88

# Tray & startup toggles: (id, label, note, default)
TRAY_TOGGLES = [
    ("startup", "Start with the session",
     "Sits in the tray so the shortcut works from login.", True),
    ("tray", "Show a tray icon",
     "Off means the shortcut is the only way in.", True),
    ("sound", "Shutter sound", "", False),
    ("recent", "Keep the last 10 snips in the tray menu",
     "Files stay on disk either way.", True),
]

# Known GNOME bindings the conflict check tests against. This is a SAMPLE —
# the real implementation must read org.gnome.desktop.wm.keybindings,
# org.gnome.settings-daemon.plugins.media-keys and the custom-keybindings
# list. See README, "Shortcut conflict check".
GNOME_KNOWN = {
    "Print":              "GNOME's \u201cTake a screenshot\u201d",
    "Shift+Print":        "GNOME's \u201cScreenshot of an area\u201d",
    "Control+Alt+T":      "GNOME's \u201cLaunch terminal\u201d",
    "Super+L":            "GNOME's \u201cLock screen\u201d",
    "Control+Alt+Delete": "GNOME's \u201cLog out\u201d",
    "Super+P":            "GNOME's \u201cSwitch monitor\u201d",
}

SHORTCUT_DEFAULT = "Control+Alt+S"

# ---------------------------------------------------------------------------
# The pre-snip chooser (design_handoff_snipux_chooser)
# ---------------------------------------------------------------------------
# Overlay furniture, not window chrome: warm glass on the 62% scrim, never the
# opaque Win palette Settings and the review window use. Everything else the
# chooser needs -- Color, Metric, Shadow, CAPTURE_MODES, AFTER_CAPTURE, DELAYS
# -- is already above. See docs/design/handoff-chooser.md.

# ---------------------------------------------------------------- geometry
class ChooserMetric:
    """The docked chooser row. Logical pixels."""

    # Panel — hangs FLUSH from the active monitor's top edge.
    # Square top corners, rounded bottom: it belongs to the edge, it does not float.
    HEIGHT           = 54          # 10 pad + 34 control + 10 pad
    WIDTH            = 420         # intrinsic; the row sizes to content
    PAD              = 10
    GAP              = 8           # between controls
    RADIUS           = (0, 0, 14, 14)   # tl, tr, br, bl
    BORDER_TOP       = 0           # no top border — it is against the edge

    # Dropdown triggers
    TRIGGER_H        = 34
    TRIGGER_RADIUS   = 9
    TRIGGER_PAD_L    = 11
    TRIGGER_PAD_R    = 9           # tighter: the chevron owns that side
    TRIGGER_ICON     = 15          # 16 for the mode trigger
    CHEVRON          = 14

    # Dropdown menus
    MENU_MODE_W      = 250
    MENU_AFTER_W     = 270         # widest — its rows carry a note line
    MENU_DELAY_W     = 152
    MENU_PAD         = 5
    MENU_RADIUS      = 11
    MENU_OFFSET_Y    = 41          # from the trigger's top; i.e. 7px below it
    MENU_ROW_PAD     = (8, 9)
    MENU_ROW_RADIUS  = 8
    MENU_ROW_ICON    = 16
    MENU_TICK        = 14

    # Hint line under the panel
    HINT_GAP         = 8
    HINT_H           = 24
    HINT_PAD         = (5, 11)
    HINT_RADIUS      = 8

    # Armed tab — what the panel collapses to
    TAB_H            = 26
    TAB_PAD_H        = 12
    TAB_RADIUS       = (0, 0, 10, 10)
    TAB_OPACITY      = 0.72        # → 1.0 on hover, 160ms ease
    TAB_BG_ALPHA     = 0.86        # slightly lighter than the panel's 0.93

    # Armed hint, centred under the tab
    ARMED_HINT_TOP   = 52
    ARMED_HINT_MS    = 180         # rise+fade in

    # Keyboard legend, bottom centre of the active monitor
    LEGEND_BOTTOM    = 26
    LEGEND_H         = 30


class ChooserColor:
    """Only what differs from Color. Everything else comes from the overlay
    palette.

    Every colour the handoff quotes with an alpha carries its `_ALPHA`
    sibling, the same pairing rule `Color` and `Win` follow -- so
    `design.chooser_color()` produces a fully-specified QColor and no caller
    ever re-types a percentage that can then drift from this file.
    """

    TRIGGER_BORDER       = "#ffffff"   # at 10% alpha
    TRIGGER_BORDER_ALPHA = 0.10
    TRIGGER_BORDER_OPEN  = "#ffffff"   # at 20% alpha -- the open dropdown's trigger
    TRIGGER_BORDER_OPEN_ALPHA = 0.20
    TRIGGER_BG_OPEN      = "#ffffff"   # at 9% alpha
    TRIGGER_BG_OPEN_ALPHA = 0.09
    MENU_BG              = "#1a1c18"   # at 98% alpha -- denser than the panel; it must be readable
    MENU_BG_ALPHA        = 0.98
    MENU_BORDER          = "#ffffff"   # at 12% alpha
    MENU_BORDER_ALPHA    = 0.12
    ROW_SELECTED_BG      = "#ffffff"   # at 8% alpha
    ROW_SELECTED_BG_ALPHA = 0.08
    ROW_SELECTED_FG      = "#f8faf0"
    ROW_IDLE_FG          = "#a8afa0"
    ROW_HOVER_BG         = "#ffffff"   # at 9% alpha
    ROW_HOVER_BG_ALPHA   = 0.09
    SHORTCUT_FG          = "#6f766a"   # the R/W/F/L glyphs in the mode menu
    MODE_ACCENT          = "#eaff7a"   # active mode's icon + the tab's mode label
    HINT_BG              = "#101210"   # at 72% alpha
    HINT_BG_ALPHA        = 0.72
    HINT_BORDER          = "#ffffff"   # at 7% alpha
    HINT_BORDER_ALPHA    = 0.07
    HINT_FG              = "#8f9689"
    WINDOW_PREVIEW       = "#e3ff4f"   # at 85% alpha
    WINDOW_PREVIEW_ALPHA = 0.85
    WINDOW_PREVIEW_FILL  = "#e3ff4f"   # at 7% alpha
    WINDOW_PREVIEW_FILL_ALPHA = 0.07
    PANEL_BG_ALPHA       = 0.93        # the panel's own fill
    LEGEND_KEY_FG        = "#d7dacb"

# Delay: label shown on the trigger. "No delay" is the stored value; the
# chooser prints it in full at this width. (The narrower icon-only variant
# abbreviates to "Off" — not used in the shipped design.)
DELAY_DEFAULT = "No delay"

# The destination menu's notes. `AFTER_CAPTURE` carries the same three
# identifiers with the Settings pane's prose, which is written for a radio
# card with a whole row to breathe in; at the chooser's 270px it overflows.
# Same decision, two surfaces, two lengths -- the identifiers stay shared so
# there is still one list of destinations.
CHOOSER_AFTER_NOTE = {
    "instant": "Straight to the clipboard, no overlay.",
    "edit": "Annotate in place, then copy or save.",
    "review": "Opens the review window to edit.",
}


# Mode → what the user does next once the mode is armed. This string is the
# hint under the panel AND the armed hint under the tab. It replaces the
# primary button that used to sit at the end of the row: picking the mode IS
# the commit, so nothing should promise an action it cannot perform.
MODE_NEXT_STEP = {
    "Region":      "Drag anywhere to frame a region",
    "Window":      "Hover a window, click to take it",
    "Full screen": "Grabs this monitor the moment you choose it",
    "Freeform":    "Draw a closed shape around anything",
}

# Mode shortcuts. Live whenever the chooser is on screen, armed or not.
MODE_KEYS = {"R": "Region", "W": "Window", "F": "Full screen", "L": "Freeform"}

# Full screen is the only mode with nothing left to aim at, so choosing it
# fires the grab immediately (after any delay). The other three arm and wait.
IMMEDIATE_MODES = ["Full screen"]


TOOL_HINTS = {
    "pen":         "Drag to draw freehand",
    "highlighter": "Sweep over the line that matters",
    "arrow":       "Drag from tail to head",
    "rect":        "Drag to box something in",
    "step":        "Click to drop the next number",
    "text":        "Click, then type into the label",
    "blur":        "Drag over anything private",
    "eraser":      "Click a mark to remove it",
    "ellipse":     "Drag to draw an oval",
    "line":        "Drag for a straight line",
    "crop":        "Drag to box off a dashed crop mark",
}
