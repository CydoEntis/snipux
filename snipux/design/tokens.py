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
    # Same red as DANGER_BG, opaque -- the recording-state dot (tray icon
    # overlay + HUD pill), which needs a solid fill rather than a hover
    # tint. A separate token rather than DANGER_BG at alpha 1.0 so
    # `design.color()`'s "colour and alpha resolve together" rule (see its
    # own docstring) still holds for callers that don't want the hover tint.
    DANGER_SOLID    = "#c85050"

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

# The third field is the note under each row in the mode menu. It says
# *what gets captured*, because Window and Full screen are the two that
# read as the same thing until you have used both -- "if you're capturing a
# window, you're capturing a full screen?" One is an application's window,
# the other is a whole monitor, and only the note distinguishes them at the
# moment of choosing.
CAPTURE_MODES = [
    ("Region",      "crop",    "Any rectangle you drag"),
    ("Window",      "window",  "One application's window"),
    ("Full screen", "monitor", "The whole monitor you are on"),
    ("Freeform",    "pen",     "A shape you draw by hand"),
]

# Notes that replace the above on the record side only, where a mode means
# something narrower than it does for a screenshot.
RECORD_MODE_NOTE = {
    # The recorder is handed a rectangle, once. Window mode picks that
    # rectangle *from* a window; it does not then follow it, so a window
    # moved mid-recording leaves the recording filming where it used to be.
    # There is no window-following in the API to build it on.
    # Short because the row elides, and a note cut off mid-sentence is
    # worse than none. The rest of the story -- that a window moved
    # mid-recording leaves the recording filming where it used to be -- is
    # in `RECORD_MODE_NEXT_STEP`, which has a whole pill to itself.
    "Window": "Films where it is right now",
}

DELAYS = ["No delay", "3s", "5s", "10s"]

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

    # Stills/record switch: a two-segment pill with a sliding highlight
    # behind whichever side is active, rather than a boolean track+knob --
    # a switch that only shows an empty knob says on/off, not on/off *what*.
    # `Win.SWITCH_W/H/KNOB/PAD` (tokens.py, Settings' opaque toggle) is the
    # naming precedent for the shape, adapted to a content-sized highlight.
    SWITCH_H         = TRIGGER_H
    SWITCH_PAD       = 3           # inset between the track edge and the highlight
    SWITCH_SEG_PAD_H = 12           # horizontal padding inside each segment


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

    # Stills/record switch. The active segment's label reuses MODE_ACCENT
    # and the idle one reuses ROW_IDLE_FG -- the same two colours the menu
    # rows already use for selected vs idle -- so only the pill graphic
    # itself needs new tokens.
    SWITCH_TRACK         = "#ffffff"   # at 6% alpha -- the pill's resting fill
    SWITCH_TRACK_ALPHA   = 0.06
    SWITCH_HIGHLIGHT     = "#ffffff"   # at 10% alpha -- behind the active side
    SWITCH_HIGHLIGHT_ALPHA = 0.10
    ROW_DISABLED_FG      = "#5c6156"   # a disabled mode row's label + icon

# Delay: the trigger's label when nothing is set, which is also the stored
# value. It was "No delay" on the trigger and "Off" in `DELAYS`, so the
# chooser carried a pair of functions whose only job was translating between
# the two; adopting the handoff's wording as the value made both of them
# identities and they are gone. Kept as a name of its own because the
# chooser asks "is a delay armed?" often enough that `!= DELAYS[0]` would
# read as an index trick rather than a question.
DELAY_DEFAULT = DELAYS[0]

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
# The next-step hint, where the record side needs a different one. The pill
# has room the menu row does not, so the caveat the note only gestures at
# gets said properly here.
RECORD_MODE_NEXT_STEP = {
    "Window": "Click a window to frame it -- moving it later will not follow",
}

MODE_KEYS = {"R": "Region", "W": "Window", "F": "Full screen", "L": "Freeform"}

# Full screen is the only mode with nothing left to aim at, so choosing it
# fires the grab immediately (after any delay). The other three arm and wait.
# On the record side nothing fires immediately -- see RECORD_DISABLED_MODES.
IMMEDIATE_MODES = ["Full screen"]

# Destinations, from the locked capture-flow handoff (docs/design/flow/).
# Merged one structure at a time as each gains a consumer rather than all at
# once: FlowMetric/FlowColor/STAGES/AUDIO_SOURCES land with the bars that
# read them.
#
# Three of the handoff's structures need no merge at all, because this file
# already carries everything in them -- split across several named
# structures rather than packed into one tuple, which is why they did not
# look merged:
#
#   * `CAPTURE_MODES` is a 4-tuple there, adding a shortcut letter and a
#     next-step hint. Both already live here, in `MODE_KEYS` and
#     `MODE_NEXT_STEP`, and `MODE_NEXT_STEP` carries the handoff's wording
#     verbatim. Folding them back into the tuple would give each of those
#     two facts a second home to drift from.
#   * `ANNOTATION_TOOLS` is (tool, letter, hint) for eight tools. Here that
#     is `TOOLS` + `SHORTCUTS` + `TOOL_HINTS`, for *eleven* -- see the note
#     on `TOOLS` for why the extra three outrank the handoff's own
#     "eight tools" rule. Adopting the handoff's list would drop them.
#   * `SHORTCUTS` is the stage-level key map there and the tool letters
#     here. Both are wanted; the stage map arrives with the bars that read
#     it, under a name that does not collide.
#
# `DELAYS` did need merging and has been: it said "Off" here and "No delay"
# on the trigger, so the chooser carried a translation pair whose only job
# was to bridge the two. The handoff's wording is the stored value now and
# that pair is gone.
#
# What this slice is for: **Copy is not the same operation for a
# recording.** A still goes on the clipboard as image data and pastes
# anywhere. A video can only go on as a file *reference* -- it pastes into
# a file manager, Slack or an upload field, and does nothing in an image
# editor or a text box. Reporting both as "Copied to the clipboard" is
# what produced "looks like its in my clip board but its hard to know
# that": true, and useless for working out what to do next.
#
# `(label, note, toast)` per kind. The handoff also gives Open, which is
# not offered on the record side yet -- see docs/design/flow/divergences.md.
DESTINATION_WORDING = {
    "instant": {
        "stills": ("Copy", "Image on the clipboard, paste anywhere.",
                   "Copied to clipboard"),
        "record": ("Copy file", "File reference -- paste into a chat or folder.",
                   "File copied -- paste into a chat or folder"),
    },
    "save": {
        "stills": ("Save", "Straight to your snips folder.", "Saved to"),
        "record": ("Save", "Straight to your recordings folder.", "Saved to"),
    },
}

# The stills/record switch, docs/design/recording.md ticket 5. UI and state
# only here -- nothing behind either side is wired to a recorder yet.
#
# `stills`, never the first entry of some list -- there is no list, just the
# two literal sides -- so the default is its own constant, the same way
# AFTER_DEFAULT/RECORD_AFTER_DEFAULT are rather than "whatever happens to be
# first".
KIND_DEFAULT = "stills"

# Freeform is close to meaningless for a recording, since video is
# rectangular -- it stays in the mode list, greyed out, with the value
# naming why rather than being hidden (handoff for this ticket).
#
# Window used to sit here too, reading "Not offered for recording yet",
# which was the honest reason: nobody had asked for it, not that anything
# stopped it. Window mode already resolves to a rect
# (`_confirm_window_pick`), and a rect is exactly what the recorder takes,
# so it needed no new machinery -- only asking for. Note the recorder films
# a fixed rectangle, so a window moved or resized mid-recording keeps
# filming the rectangle it started in.
# Full screen has nothing left to aim at on the stills side, but on the
# record side there is equally nothing *downstream* wired up yet, so it must
# not fire immediately there -- it arms and waits like Region does.
RECORD_DISABLED_MODES = {
    "Freeform": "Video is rectangular",
}

# The record side's "then" vocabulary: Copy, Save and Open -- never Edit or
# Review, because there is no annotate-in-place for a video. "open" is the
# third destination this file long said would land "once editing exists":
# it exists now, as `player.PlayerWindow`, and it is where a recording goes
# to be trimmed and exported. "save" and "open" are destination ids that
# exist only here, not in `AFTER_CAPTURE` (stills-only).
#
# These two are genuine alternatives, and the wording says which is which.
# They were not always: landing a recording used to move the file into the
# save folder *unconditionally* and only then consider `after`, so "instant"
# both copied to the clipboard and left a file behind, while "save" was a
# no-op that took credit for the move. A user who chose the clipboard got a
# file anyway, in a folder they were never shown, and nothing in either
# label said so.
CHOOSER_RECORD_AFTER_NOTE = {
    "instant": "Copy to the clipboard. No file is kept.",
    "save": "Save to your recordings folder.",
    "open": "Save, then open it to trim and export.",
}
RECORD_AFTER_DEFAULT = "instant"

# The same two, for Settings' own Recording pane to read down -- the
# `(id, label, note)` shape `AFTER_CAPTURE` already uses, so RadioCard
# renders both panes from one structure. Recording's destination had no
# Settings row at all before this: it could only be set on the chooser,
# per-capture, with no way to say what it should default to.
RECORDING_AFTER = [
    ("instant", "Copy to the clipboard",
     "The finished video goes to the clipboard and the file is deleted. "
     "Paste it somewhere that accepts a file."),
    ("save", "Save to a folder",
     "The finished video is moved into your recordings folder, under the "
     "filename pattern below."),
    ("open", "Open in the player",
     "Saved as above, then opened in the trim editor -- play it back, cut "
     "the dead air off either end and export."),
]

# Recordings get their own default name, not the stills one. Sharing
# FILENAME_DEFAULT meant a video landed called "Screenshot from
# 2026-08-27 15-54-01.mp4" -- the wrong noun for the thing, in a folder
# full of actual screenshots.
RECORDING_FILENAME_DEFAULT = "Recording from %Y-%m-%d %H-%M-%S"

# Settings' Saving pane (recording.md ticket 9): what a GNOME recording asks
# `org.gnome.Shell.Screencast` for absent any stored preference. 30 is a
# plain, ordinary default frame rate -- not a measurement of anything, unlike
# WindowsRecorderBackend's own rate (SNX-125), which is real inter-arrival
# timing and deliberately never touches this constant or the Settings row
# behind it.
RECORDING_FRAME_RATE_DEFAULT = 30

# recording.md ticket 9's disk-space guard: below this many free bytes on
# the save folder's filesystem, `AppController` stops the active recording
# rather than let it run the disk to zero. Not user-configurable and not a
# measurement of anything -- a plain, generous floor (a few seconds of even
# a large full-screen capture) meant to leave headroom for landing the file
# itself (the move/copy in `AppController._land_recording`) to still fit.
RECORDING_MIN_FREE_BYTES = 200 * 1024 * 1024

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


# ---------------------------------------------------------------------------
# The capture flow's bars (docs/design/flow, LOCKED 2026-08-27)
# ---------------------------------------------------------------------------
# Overlay furniture on the same warm glass as the chooser, never the opaque
# Win palette. The handoff is emphatic that this is one palette with
# tokens.py rather than a fork -- everything not restated here (Color,
# Metric, Shadow, Font) still comes from above.
#
# Three rules the handoff says are load-bearing, each arrived at by building
# the alternative and rejecting it. Metrics here only make sense with them:
#
#   1. Every bar is centred -- the chooser on the monitor, every
#      post-selection bar on the selection. Never edge-anchored: a centred
#      bar that changes width moves both edges, which is also why
#   2. nothing collapses. Tools stay visible.
#   3. The primary action is at the LEFT end, before a divider, and is the
#      only accent-filled control in the bar.

# ---------------------------------------------------------------- geometry
class FlowMetric:
    """Shared by the chooser row, the stills bar and the recording bar."""

    ROW_H            = 42          # 6 pad + 28 control + 6 pad + 2x1px border
    PAD              = 6
    GAP              = 3           # between icon buttons
    GROUP_GAP        = 7           # either side of the action divider
    RADIUS           = 12          # free-floating bars
    RADIUS_DOCKED    = (0, 0, 12, 12)   # chooser: flush to the monitor's top edge

    BTN              = 28          # every control in every bar
    BTN_RADIUS       = 8
    ICON             = 16          # glyph in a 28px button; 15 in a labelled chip
    ICON_STROKE      = 1.55
    CHEVRON          = 12
    DIVIDER_H        = 20

    # Split action button
    SPLIT_PAD_H      = 11
    SPLIT_CARET_W    = 22


    # Placement, relative to the SELECTION -- not the screen. Rule 1.
    BAR_OFFSET_Y     = 16          # gap below the selection's bottom edge
    BAR_EDGE_MARGIN  = 12          # min gap from a monitor edge after clamping
    BAR_BOTTOM_ROOM  = 108         # bar top is clamped to monitor_h - this

    # Hint line under every bar
    HINT_GAP         = 7
    HINT_PAD         = (4, 10)
    HINT_RADIUS      = 7
    HINT_ICON        = 12

    # Dropdown popovers
    MENU_PAD         = 4
    MENU_RADIUS      = 11
    MENU_OFFSET      = 6           # from the trigger edge
    MENU_ROW_PAD     = (7, 8)
    MENU_ROW_RADIUS  = 7
    MENU_W_MODE      = 228
    MENU_W_DEST      = 284
    MENU_W_AUDIO     = 250
    MENU_W_DELAY     = 146

    # Chips above the selection
    CHIP_OFFSET_Y    = 34
    CHIP_RADIUS      = 7
    CHIP_PAD         = (5, 10)

    # Selection frame. FRAME_INSET is negative because the frame is drawn
    # OUTSIDE the captured pixels -- a border on the boundary would land in
    # the file.
    FRAME_W          = 2
    FRAME_INSET      = -3
    ANTS_DASH        = (7, 7)
    ANTS_PERIOD_MS   = 700
    CORNER_LEN       = 24
    CORNER_W         = 4
    HANDLE_LONG      = 30
    HANDLE_SHORT     = 8

    # How long the finished bar stays up after a recording lands. Long
    # enough to read a summary and reach for Discard, short enough not to
    # sit there. Not from the handoff: the handoff's stage 6 waits for the
    # user to confirm a destination, and this build lands the file first --
    # see docs/design/flow/divergences.md.
    DONE_LINGER_MS   = 6000

    # Countdown, centred IN the region -- where the user is already looking.
    COUNT_D          = 118
    COUNT_FONT       = 54

    # A drag smaller than this is discarded rather than captured. Note this
    # is the handoff's figure; TODO.md records a 16x16 minimum as an earlier
    # deliberate deviation, so whichever ends up enforced, only one of these
    # two numbers may be live at a time.
    MIN_SEL_W        = 60
    MIN_SEL_H        = 40


class FlowColor:
    """Only what differs from Color / ChooserColor.

    Alphas ride as `<TOKEN>_ALPHA` siblings so `design.flow_color()` can hand
    back one fully-specified QColor, the same pairing rule the other three
    palettes follow -- a caller must never re-type a percentage into an
    rgba() string that then drifts from the token.
    """

    BAR_BG               = "#1a1c18"
    BAR_BG_ALPHA         = 0.93
    BAR_BORDER           = "#ffffff"
    BAR_BORDER_ALPHA     = 0.10
    BAR_BORDER_LIVE      = "#ff5a52"
    BAR_BORDER_LIVE_ALPHA = 0.34

    MENU_BG              = "#1a1c18"
    MENU_BG_ALPHA        = 0.98
    MENU_BORDER          = "#ffffff"
    MENU_BORDER_ALPHA    = 0.12
    ROW_SELECTED_BG      = "#ffffff"
    ROW_SELECTED_BG_ALPHA = 0.08
    ROW_SELECTED_FG      = "#f8faf0"
    ROW_IDLE_FG          = "#a8afa0"
    ROW_HOVER_BG         = "#ffffff"
    ROW_HOVER_BG_ALPHA   = 0.09
    ROW_NOTE_FG          = "#8f9689"
    SECTION_FG           = "#616a5c"
    SHORTCUT_FG          = "#6f766a"

    TOOL_ACTIVE_BG       = "#ffffff"
    TOOL_ACTIVE_BG_ALPHA = 0.16
    TOOL_ACTIVE_FG       = "#f8faf0"
    TOOL_IDLE_FG         = "#a8afa0"
    TOOL_DISABLED_FG     = "#5d6157"
    DANGER_BG            = "#c85050"
    DANGER_BG_ALPHA      = 0.22
    DANGER_FG            = "#f5a3a3"

    ACCENT               = "#e3ff4f"
    ACCENT_FG            = "#15170e"
    # The 1px line between a split button's face and its caret. Which half
    # a click lands in has to be visible before the click, or a split
    # button is just a button that sometimes does something else.
    SPLIT_SEAM           = "#15170e"
    SPLIT_SEAM_ALPHA     = 0.22
    ACCENT_SOFT          = "#eaff7a"   # accent as TEXT or a small glyph
    # The handoff gives this as "14-18% for an armed segment"; the prototype
    # spends the range on two different things, so it is two tokens here
    # rather than one that has to be right twice. .18 is the armed kind
    # segment; PAUSE_WASH is the paused button in the live bar.
    ACCENT_WASH          = "#e3ff4f"
    ACCENT_WASH_ALPHA    = 0.18
    PAUSE_WASH           = "#e3ff4f"
    PAUSE_WASH_ALPHA     = 0.14

    # Recording. The only place red appears in the product, which is what
    # lets it mean "live" without a label -- do not spend it anywhere else.
    REC                  = "#ff5a52"
    REC_FG               = "#2a0d0b"   # text on the Stop button
    REC_CLOCK            = "#ffd9d6"
    REC_WASH             = "#ff5a52"
    REC_WASH_ALPHA       = 0.14

    SCRIM                = "#0c0d0a"
    SCRIM_ALPHA          = 0.62
    SCRIM_LIVE_ALPHA     = 0.28        # drops so you can see what you are filming

    # Window-mode hover preview: two alphas on one colour, so they are two
    # tokens.
    WINDOW_HOVER         = "#e3ff4f"
    WINDOW_HOVER_ALPHA   = 0.85        # the 2px border
    WINDOW_HOVER_FILL    = "#e3ff4f"
    WINDOW_HOVER_FILL_ALPHA = 0.07


# Audio sources for the recording bar's dropdown. `AUDIO_DEFAULT` is the one
# every platform can honour: `org.gnome.Shell.Screencast` has no audio option
# at all, so on Linux the other two are offered disabled with the reason
# rather than hidden -- see docs/design/flow/divergences.md 2. A control that
# opens a menu it cannot act on is the failure the handoff names elsewhere,
# and silently dropping the options is the same lie told quietly.
AUDIO_SOURCES = [
    ("system", "speaker", "System", "Desktop output -- what you hear"),
    ("mic",    "mic",     "Mic",    "Default input device"),
    ("off",    "mute",    "Muted",  "No audio track at all"),
]
AUDIO_DEFAULT = "off"

# Stage -> (label, the hint line under the bar). The label names the state
# machine's own phase; the hint is what the user does next. Two of these are
# placeholders in the handoff ("<mode hint from CAPTURE_MODES>") because the
# text belongs to whatever is armed, so those read from MODE_NEXT_STEP and
# TOOL_HINTS instead and are absent here rather than duplicated wrongly.
STAGES = {
    "choose":   ("Choose", "pick a mode, then drag a region"),
    "recArmed": ("Ready to record",
                 "Reframe now -- you cannot resize once it is rolling"),
    "count":    ("Counting down", "Recording starts -- Esc to stop"),
    "live":     ("Recording", "This bar sits outside the recorded frame"),
    "done":     ("Finished", "Trim and export in the player"),
}


# --------------------------------------------------------------------------
# Recording player / trim editor (docs/design/player, LOCKED 2026-08-27)
#
# The player wears the REVIEW window's chrome, not the overlay's glass, so
# `Win` / `WinMetric` above cover the shell, title bar and footer. What
# follows is only what the player adds: the floating transport and the
# timeline rail. `Gradient.WORKSPACE` is already the radial the handoff
# specifies for the canvas, so it is reused rather than restated.
# --------------------------------------------------------------------------

class PlayerMetric:
    """Player geometry. Logical pixels."""
    WINDOW_MIN       = (980, 640)

    # Floating transport, over the canvas bottom -- the annotate bar's shell
    BAR_H            = 42          # 6 pad + 28 control + 6 pad + borders
    BAR_PAD          = 6
    BAR_GAP          = 3
    BAR_RADIUS       = 12
    BAR_BOTTOM       = 18          # from the canvas floor
    BTN              = 28
    BTN_RADIUS       = 8
    ICON             = 16

    # Video frame on the workspace
    FRAME_BORDER     = 1
    FRAME_RING       = 7           # rgba(255,255,255,.02)
    BADGE_INSET      = (16, 14)
    ZOOM_STEPS       = (60, 160, 20)
    PLAY_OVERLAY_D   = 74          # centre play badge while paused

    # Timeline panel
    PANEL_PAD        = (12, 16, 14)
    PANEL_GAP        = 9
    RAIL_H           = 96
    RAIL_RADIUS      = 9
    RULER_H          = 16
    FILMSTRIP_H      = 44
    WAVE_H           = 36          # rail minus ruler minus filmstrip
    FILMSTRIP_CELLS  = 16
    WAVE_BARS        = 120
    TICK_EVERY_S     = 5

    HANDLE_HIT_W     = 14          # invisible grab area
    HANDLE_W         = 8           # visible bar
    HANDLE_H         = 34
    HANDLE_RADIUS    = 3
    PLAYHEAD_W       = 2
    RANGE_EDGE_W     = 2
    MIN_RANGE_S      = 0.5

    ROW_BTN_H        = 26          # Start here / End here / Reset
    ACTION_BTN_H     = 36          # footer buttons
    ACTION_RADIUS    = 9
    SPLIT_CARET_W    = 24


class PlayerColor:
    RAIL_BG          = "#14161a"
    RAIL_BORDER      = "#262a31"
    RULER_RULE       = "#1f2229"
    TICK             = "#2f333b"
    TICK_FG          = "#5f6674"

    FILM_CELL        = "#26271f"        # the recorded content's own tone
    FILM_SEAM        = "#000000"        # at 35%
    OUTSIDE_OPACITY  = 0.38             # filmstrip cells outside the range
    OUTSIDE_VEIL     = "#0a0b0d"        # at 72%, over ruler-to-bottom

    WAVE_IN          = "#c8d96a"        # inside the range, audio kept
    WAVE_OUT         = "#4a4f45"        # outside the range
    WAVE_MUTED_IN    = "#3a3f47"        # muted: the whole waveform greys
    WAVE_MUTED_OUT   = "#23262d"

    TRIM             = "#e3ff4f"        # range edges + both handles
    TRIM_INNER       = "#e3ff4f"        # at 18%, inset ring
    HANDLE_GRIP      = "#15170e"        # at 50%, the 2x14 line in the handle
    PLAYHEAD         = "#ff5a52"        # red = "now", matching the recording bar
    PLAYHEAD_FG      = "#2a0d0b"        # text in the playhead's time flag

    KEPT_FG          = "#c8d96a"        # "keeping 00:20"
    CUT_FG           = "#c8a54a"        # "-00:07 cut"
    MUTED_FG         = "#f5a3a3"
    MUTED_BG         = "#c85050"        # at 20%

    SAVED_FG         = "#9ec46a"
    DIRTY_FG         = "#c8a54a"

    # Transport shell + controls, over the canvas
    BAR_BG           = "#1a1c18"        # at 94%
    BAR_BORDER       = "#ffffff"        # at 10%
    BAR_SEP          = "#ffffff"        # at 12%
    BTN_IDLE_FG      = "#a8afa0"
    BTN_ON_BG        = "#ffffff"        # at 12%, the pre-lit play button
    BTN_HOVER_BG     = "#ffffff"        # at 9%
    BTN_ON_FG        = "#f1f3e8"
    TIME_FG          = "#f1f3e8"
    TIME_TOTAL_FG    = "#6f766a"
    ACCENT_ON_BG     = "#e3ff4f"        # at 15%, loop/speed when engaged
    ACCENT_ON_FG     = "#eaff7a"
    MENU_BG          = "#1a1c18"        # at 98%

    PAUSE_SCRIM      = "#0c0d0a"        # at 28%, over the frame while paused
    PAUSE_BADGE_BG   = "#141612"        # at 82%
    PAUSE_BADGE_EDGE = "#ffffff"        # at 16%
    PAUSE_BADGE_FG   = "#f1f3e8"

    BADGE_BG         = "#121418"        # at 88%, canvas corner badges
    BADGE_BORDER     = "#262a31"
    BADGE_FG         = "#9aa2b1"
    BADGE_SEP        = "#4e545f"
    ZOOM_FG          = "#aeb5c2"
    ZOOM_BTN_FG      = "#8a92a1"
    ZOOM_BTN_HOVER   = "#282c34"


# Playback speeds. 1x is the only one that renders without the accent tint --
# an altered speed must be visible without reading the number.
SPEEDS = ["0.5", "1", "1.5", "2"]

# Export formats: id, icon, label, the one-line consequence, MB/s estimate.
# `frame` has no per-second figure because it is a single still.
EXPORT_FORMATS = [
    ("webm",  "save",   "WebM",                 "What was recorded -- no re-encode when untrimmed.", 0.42),
    ("mp4",   "save",   "MP4 (H.264)",          "Plays anywhere. Slack, Teams, browsers.",           0.55),
    ("gif",   "image",  "GIF",                  "Silent, loops. Big above ~10 seconds.",             1.90),
    ("frame", "camera", "Current frame as PNG", "Just the frame under the playhead.",                None),
]
EXPORT_DEFAULT = "mp4"
EXPORT_FRAME_MB = 0.9

# The footer's primary is EXPORT, not Copy: trimming re-encodes, so a file must
# be written and a clipboard-only result would be a lie. Copy stays secondary
# and copies a file REFERENCE, the same rule as the capture flow.
EXPORT_FOOTNOTE = ("Trimming re-encodes. The untrimmed original stays at its "
                   "own path until you overwrite it.")

PLAYER_FPS = 30

PLAYER_SHORTCUTS = {
    "Space": "play / pause",
    "I": "set the start at the playhead",
    "O": "set the end at the playhead",
    "Left": "previous frame",
    "Right": "next frame",
    "M": "mute (drops the audio track on export)",
    "L": "loop the trimmed range",
    "Esc": "close an open menu",
}
