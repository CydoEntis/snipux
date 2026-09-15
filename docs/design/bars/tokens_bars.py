"""snipux — chooser + stills bar tokens (LOCKED 2026-09-15).

Supersedes the chooser and stills-bar sections of tokens_flow.py. The capture
flow's recording bars, stage machine and destination semantics are unchanged —
keep using design_handoff_snipux_flow/ for those.

Overlay furniture: warm glass (#1a1c18 at 94%) on the 62% scrim. Never the
opaque Win chrome used by Settings and the review window.

THE CONTROL-WEIGHT RULE — this is what keeps both bars small. Every control in
either bar is one of three weights, and the weight is decided by how often the
user re-makes the decision:

    labelled chip   decided EVERY snip          → mode (the only one)
    icon button     decided OCCASIONALLY        → destination, flags, tools
    menu row        set and FORGOTTEN           → Last region, watermark text

A control's job is to SHOW ITS STATE, not to explain itself. Explanation goes in
the tooltip and the hint line. That is also what frees the accent (#e3ff4f) to
mean exactly one thing: "this is the thing you press".

THE EIGHT-SLOT BUDGET — the stills bar holds eight tool slots and no more. A new
feature must answer one of three questions before it ships:

    which existing slot does it join as a sibling?   (→ notch family)
    which popover section does it belong to?          (→ per-tool style)
    is it really a Settings preference?                (→ out of the bar)

Nothing gets a slot for being new. Rounded-rect, polygon, callout, spotlight are
all shape siblings. Arrow-head style, corner radius, text weight are popover.
Default ink and watermark content are Settings.
"""

# ---------------------------------------------------------------- geometry
class BarMetric:
    """Both bars. Logical pixels."""
    ROW_H            = 42          # 6 pad + 28 control + 6 pad + 2×1px border
    PAD              = 6
    GAP              = 3
    RADIUS           = 12                  # free-floating (stills bar)
    RADIUS_DOCKED    = (0, 0, 12, 12)      # chooser, flush to the monitor top
    BTN              = 28
    BTN_RADIUS       = 8
    ICON             = 15
    ICON_STROKE      = 1.55
    CHEVRON          = 12
    DIVIDER_H        = 20
    DIVIDER_MARGIN   = 4

    WELL_PAD         = 2           # recessed group (kind pair, flag pair)
    WELL_RADIUS      = 9
    WELL_BTN_RADIUS  = 7

    NOTCH            = 9           # corner hit area on a family slot
    NOTCH_TRIANGLE   = 5           # visible triangle leg

    SPLIT_PAD_H      = 10
    SPLIT_CARET_W    = 18
    SPLIT_SEAM       = "#15170e"   # at 22%

    # Measured widths of the shipped layouts
    CHOOSER_W        = 382
    STILLS_W         = 478

    # Collapsed chooser tab (armed / marking up)
    TAB_H            = 22
    TAB_PAD_H        = 11
    TAB_RADIUS       = (0, 0, 10, 10)
    TAB_OPACITY      = 0.70        # → 1.0 on hover, 160ms

    # Hint pill under either bar
    HINT_GAP         = 7
    HINT_PAD         = (4, 10)
    HINT_RADIUS      = 7
    HINT_ICON        = 12

    # Popovers
    MENU_PAD         = 4
    MENU_RADIUS      = 11
    MENU_OFFSET      = 6
    MENU_ROW_PAD     = (7, 8)
    MENU_ROW_RADIUS  = 7
    MENU_W_MODE      = 240
    MENU_W_SHAPES    = 186
    MENU_W_REDACT    = 244         # widest: rows carry a security note
    MENU_W_STYLE     = 216
    MENU_W_WATERMARK = 238

    # Bar placement — CENTRED on the selection, clamped to the monitor
    BAR_OFFSET_Y     = 16
    BAR_EDGE_MARGIN  = 12
    BAR_BOTTOM_ROOM  = 108
    MIN_SEL_W        = 80
    MIN_SEL_H        = 50


class BarColor:
    BAR_BG               = "#1a1c18"   # at 94%
    BAR_BORDER           = "#ffffff"   # at 10%
    DIVIDER              = "#ffffff"   # at 12%
    WELL_BG              = "#000000"   # at 34%
    MENU_BG              = "#1a1c18"   # at 98%
    MENU_BORDER          = "#ffffff"   # at 12%
    ROW_SELECTED_BG      = "#ffffff"   # at 8%
    ROW_SELECTED_FG      = "#f8faf0"
    ROW_IDLE_FG          = "#a8afa0"
    ROW_HOVER_BG         = "#ffffff"   # at 9%
    ROW_NOTE_FG          = "#8f9689"
    SECTION_FG           = "#616a5c"
    SHORTCUT_FG          = "#6f766a"

    TOOL_ACTIVE_BG       = "#ffffff"   # at 16%
    TOOL_ACTIVE_FG       = "#f8faf0"
    TOOL_IDLE_FG         = "#a8afa0"
    TOOL_DISABLED_FG     = "#5d6157"   # undo, empty stack
    FLAG_OFF_FG          = "#8f9689"   # unlit flag inside a well
    DANGER_BG            = "#c85050"   # at 22%
    DANGER_FG            = "#f5a3a3"

    ACCENT               = "#e3ff4f"
    ACCENT_FG            = "#15170e"
    ACCENT_SOFT          = "#eaff7a"   # accent as text or a small glyph
    ACCENT_WASH          = "#e3ff4f"   # at 18% — an armed flag's fill

    STYLE_DOT_BG         = "#ffffff"   # at 6%; 14% when its popover is open
    DISABLED_OPACITY     = 0.34        # style dot for blackout / eraser

    HINT_BG              = "#101210"   # at 78%
    HINT_FG              = "#7d8478"
    SCRIM                = "#0c0d0a"
    SCRIM_CHOOSE         = 0.62
    SCRIM_MARKUP         = 0.72        # a touch darker once a region exists
    WINDOW_HOVER         = "#e3ff4f"   # 85% border / 7% fill


# ---------------------------------------------------------------- chooser
# Labelled chip — the ONLY label in the row.
CAPTURE_MODES = [
    ("Region",      "crop",    "R", "Drag anywhere to frame a region"),
    ("Window",      "window",  "W", "Hover a window, click to take it"),
    ("Full screen", "monitor", "F", "Grabs this monitor the moment you choose it"),
    ("Freeform",    "pen",     "L", "Draw a closed shape around anything"),
]
IMMEDIATE_MODES = ["Full screen"]

# Last region is a MODE, not a flag — it answers "what to capture". It lives
# below a rule in the mode menu and shows the dimensions it restores.
LAST_REGION = {"glyph": "undo", "label": "Last region", "shortcut": "Shift+R",
               "subtitle": "<w> × <h> of the previous capture"}

# Icon button — the chooser sets the DEFAULT; the stills bar's split button
# restates it, so no label is needed here.
DESTINATIONS = [
    ("Copy", "copy", "C", "Image on the clipboard"),
    ("Save", "save", "S", "Straight to ~/Pictures/snipux"),
    ("Edit", "eye",  "O", "Opens the review window"),
]

# Flags. Both live in ONE recessed well matching the kind pair, so an unlit flag
# reads as OFF rather than as a button nobody has pressed yet.
FLAGS = [
    ("hide",  "eyeOff", "Hide sensitive",
     "Blurs password fields, tokens and card numbers on capture."),
    ("delay", "timer",  "Delay", "Countdown before the grab."),
]
DELAYS = ["Off", "3s", "5s", "10s"]

# The eye-with-strike is deliberate: the droplet is the BLUR TOOL's glyph and
# says "I will smudge this by hand", not "the app finds and masks secrets".
HIDE_SENSITIVE_GLYPH = "eyeOff"


# ---------------------------------------------------------------- stills bar
# Order is a GRADIENT of consequence, left to right:
#   draw on top → frame → add content → destroy pixels → remove marks
# Never MRU-sorted. Never re-ordered at runtime. Muscle memory is the feature.
TOOL_ORDER = ["pen", "highlighter", "SHAPES", "step", "text", "REDACT", "eraser"]

SHAPES = [
    ("rect",    "Rectangle",     "R"),
    ("ellipse", "Ellipse",       "O"),
    ("line",    "Straight line", "L"),
    ("arrow",   "Arrow",         "A"),
]

# Three modes, and the note is not decoration: blur on small text is famously
# recoverable, so the row has to say what each one actually guarantees.
REDACTIONS = [
    ("blur",     "blur",     "Blur",     "Softens it — shapes still readable"),
    ("pixelate", "mask",     "Pixelate", "Blocky, obviously deliberate"),
    ("blackout", "blackout", "Blackout", "Solid bar. Nothing to reconstruct"),
]
BLACKOUT_FILL = "#0b0c09"

ANNOTATION_TOOLS = [
    ("pen",         "P", "Drag to draw freehand"),
    ("highlighter", "H", "Sweep over the line that matters"),
    ("rect",        "R", "Drag to box something in"),
    ("ellipse",     "O", "Drag to ring something"),
    ("line",        "L", "Drag for a straight line"),
    ("arrow",       "A", "Drag from tail to head"),
    ("step",        "S", "Click to drop the next number"),
    ("text",        "T", "Click, then type into the label"),
    ("blur",        "B", "Drag over anything private"),
    ("pixelate",    "B", "Drag over anything private"),
    ("blackout",    "B", "Drag to black it out completely"),
    ("eraser",      "E", "Click a mark to remove it"),
]

INK_SWATCHES = [
    ("Acid",    "#e3ff4f"), ("Red",     "#ef4444"), ("Sky",   "#38bdf8"),
    ("Emerald", "#10b981"), ("Violet",  "#a855f7"), ("White", "#ffffff"),
    ("Ink",     "#12141a"),
]

# Style is PER TOOL and REMEMBERED. Setting a dashed red box must not turn the
# pen red. Ship these as the first-run defaults.
DEFAULT_STYLE = {
    "pen":         {"color": "#e3ff4f", "size": 5, "dash": "solid", "fill": "outline"},
    "highlighter": {"color": "#f59e0b", "size": 5, "dash": "solid", "fill": "outline"},
    "rect":        {"color": "#ef4444", "size": 3, "dash": "dashed", "fill": "both"},
    "ellipse":     {"color": "#38bdf8", "size": 3, "dash": "solid", "fill": "outline"},
    "line":        {"color": "#e3ff4f", "size": 3, "dash": "solid", "fill": "outline"},
    "arrow":       {"color": "#ef4444", "size": 3, "dash": "solid", "fill": "outline"},
    "step":        {"color": "#ef4444", "size": 5, "dash": "solid", "fill": "filled"},
    "text":        {"color": "#ffffff", "size": 5, "dash": "solid", "fill": "outline"},
}

# Cycling controls: click advances, the control shows the current state. Same
# pattern as the chooser's destination and delay — one behaviour to learn.
FILL_CYCLE = [("outline", "Outline only"), ("filled", "Filled"), ("both", "Outline and filled")]
DASH_CYCLE = [("solid", "none", "Solid"), ("dashed", "9 7", "Dashed"), ("dotted", "2 5", "Dotted")]
FILL_OPACITY = {"outline": 0.0, "filled": 0.90, "both": 0.22}   # of the STROKE colour

# Which popover sections a tool shows. Anything absent is not rendered — never
# rendered-but-inert. Tools with no styleable property at all (blackout,
# eraser) dim the style dot to DISABLED_OPACITY and it stops opening.
STYLE_SECTIONS = {
    "pen":         ["color", "size"],
    "highlighter": ["color", "size"],
    "rect":        ["color", "fill", "dash", "size"],
    "ellipse":     ["color", "fill", "dash", "size"],
    "line":        ["color", "dash", "size"],
    "arrow":       ["color", "dash", "size"],
    "step":        ["color", "size"],
    "text":        ["color", "size"],
    "blur":        ["strength"],
    "pixelate":    ["strength"],
    "blackout":    [],
    "eraser":      [],
}
STROKE_RANGE   = (1, 26)
STRENGTH_RANGE = (2, 20)

# The watermark is NOT a tool — it applies to the whole image, once, and the
# user needs to see where it lands. Toggle + notch for placement; the mark
# itself (logo, text, colour) is a Settings preference.
WATERMARK = {
    "glyph": "layers",
    "corners": ["tl", "tr", "bl", "br"],
    "default_corner": "br",
    "inset": 14,
    "opacity_range": (20, 100),
    "default_opacity": 70,
    "content_lives_in": "Settings",
}

SHORTCUTS = {
    "R": "mode Region / shape Rectangle", "W": "mode Window",
    "F": "mode Full screen", "L": "mode Freeform / shape Line",
    "Shift+R": "Last region",
    "P": "pen", "H": "highlighter", "O": "ellipse", "A": "arrow",
    "S": "numbered step", "T": "text", "B": "redaction", "E": "eraser",
    "1-7": "ink colour", "[": "thinner", "]": "thicker", "D": "cycle line style",
    "Space": "reopen the chooser",
    "Enter": "fire the destination",
    "Esc": "close a menu, else step back",
    "Ctrl+Z": "undo",
}
