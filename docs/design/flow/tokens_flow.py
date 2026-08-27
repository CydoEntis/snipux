"""snipux — capture flow tokens (LOCKED 2026-08-27).

Additions for the capture flow: the chooser row, the post-selection stills bar,
and the recording bar. Merge into the main handoff's tokens.py — do not fork the
palette.

Everything here is overlay furniture: warm glass (#1a1c18 at 93%) on the 62%
scrim, never the opaque Win chrome that Settings and the review window use.
Colours, radii and metrics not listed here come from tokens.py (Color, Metric,
Shadow) and tokens_chooser.py.

THE THREE RULES THAT SURVIVED ITERATION — break these and the design stops working:

  1. EVERY BAR IS CENTRED ON THE REGION (the chooser is centred on the MONITOR).
     Not right-anchored, not left-anchored. Bars must never shift sideways
     between stages.
  2. NO COLLAPSING. Tools are always visible. A collapse mechanic was designed,
     built and rejected: with a centred bar any width change moves both edges.
  3. THE PRIMARY ACTION IS AT THE LEFT END, before the tools, with a divider
     after it. It is the only accent-filled control in the bar, and picking a
     tool never changes anything to its left.
"""

# ---------------------------------------------------------------- geometry
class FlowMetric:
    """Shared by the chooser row, the stills bar and the recording bar."""

    ROW_H            = 42          # 6 pad + 28 control + 6 pad + 2×1px border
    PAD              = 6
    GAP              = 3           # between icon buttons
    GROUP_GAP        = 7           # either side of the action divider
    RADIUS           = 12          # free-floating bars
    RADIUS_DOCKED    = (0, 0, 12, 12)   # chooser: flush to the monitor's top edge

    BTN              = 28          # every control in every bar
    BTN_RADIUS       = 8
    ICON             = 16          # glyph in a 28px button; 15 in a labelled chip
    ICON_STROKE      = 1.55
    CHEVRON          = 12          # on a dropdown trigger
    DIVIDER_H        = 20
    DIVIDER          = "#ffffff"   # at 12% alpha

    # Split action button
    SPLIT_PAD_H      = 11
    SPLIT_CARET_W    = 22
    SPLIT_SEAM       = "#15170e"   # at 22% alpha — 1px between face and caret

    # Bar placement, relative to the SELECTION (not the screen)
    BAR_OFFSET_Y     = 16          # gap below the selection's bottom edge
    BAR_EDGE_MARGIN  = 12          # min gap from a monitor edge after clamping
    BAR_BOTTOM_ROOM  = 108         # bar top is clamped to monitor_h − this

    # Hint line under every bar
    HINT_GAP         = 7
    HINT_PAD         = (4, 10)
    HINT_RADIUS      = 7
    HINT_BG          = "#101210"   # at 72%
    HINT_FG          = "#7d8478"
    HINT_ICON        = 12          # accent-coloured, states the active tool/mode

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

    # Chips above the selection (dimension chip, Frozen pill)
    CHIP_OFFSET_Y    = 34
    CHIP_RADIUS      = 7
    CHIP_PAD         = (5, 10)

    # Selection frame
    FRAME_W          = 2
    FRAME_INSET      = -3          # drawn OUTSIDE the captured pixels
    ANTS_DASH        = (7, 7)
    ANTS_PERIOD_MS   = 700
    CORNER_LEN       = 24
    CORNER_W         = 4
    HANDLE_LONG      = 30
    HANDLE_SHORT     = 8

    # Countdown
    COUNT_D          = 118         # circle diameter, centred IN the region
    COUNT_FONT       = 54

    MIN_SEL_W        = 60          # a smaller drag is discarded, not captured
    MIN_SEL_H        = 40


class FlowColor:
    """Only what differs from Color / ChooserColor."""
    BAR_BG               = "#1a1c18"   # at 93%
    BAR_BORDER           = "#ffffff"   # at 10%
    BAR_BORDER_LIVE      = "#ff5a52"   # at 34% — recording only
    MENU_BG              = "#1a1c18"   # at 98%
    MENU_BORDER          = "#ffffff"   # at 12%
    ROW_SELECTED_BG      = "#ffffff"   # at 8%
    ROW_SELECTED_FG      = "#f8faf0"
    ROW_IDLE_FG          = "#a8afa0"
    ROW_HOVER_BG         = "#ffffff"   # at 9%
    ROW_NOTE_FG          = "#8f9689"
    SECTION_FG           = "#616a5c"   # uppercase menu heading
    SHORTCUT_FG          = "#6f766a"

    TOOL_ACTIVE_BG       = "#ffffff"   # at 16%
    TOOL_ACTIVE_FG       = "#f8faf0"
    TOOL_IDLE_FG         = "#a8afa0"
    TOOL_DISABLED_FG     = "#5d6157"   # undo with an empty stack
    DANGER_BG            = "#c85050"   # at 22% — clear-ink hover
    DANGER_FG            = "#f5a3a3"

    ACCENT               = "#e3ff4f"   # the one primary; action button + armed mode
    ACCENT_FG            = "#15170e"
    ACCENT_SOFT          = "#eaff7a"   # accent as TEXT or a small glyph
    ACCENT_WASH          = "#e3ff4f"   # at 14–18% for an armed segment

    # Recording — the only place red appears in the product, which is why it can
    # mean "live" without a label.
    REC                  = "#ff5a52"
    REC_FG               = "#2a0d0b"   # text on the Stop button
    REC_CLOCK            = "#ffd9d6"
    REC_WASH             = "#ff5a52"   # at 14% behind the clock

    SCRIM                = "#0c0d0a"   # 62% while choosing/aiming/marking
    SCRIM_LIVE_ALPHA     = 0.28        # drops so you can see what you are filming
    SCRIM_ALPHA          = 0.62

    WINDOW_HOVER         = "#e3ff4f"   # 85% border / 7% fill, Window mode preview


# ---------------------------------------------------------------- content
# Capture kind — the leading segmented pair in the chooser.
# Record renders as a filled 10px circle, NOT an icon glyph.
CAPTURE_KINDS = [("stills", "camera", "Still image"),
                 ("record", None, "Screen recording")]

CAPTURE_MODES = [
    ("Region",      "crop",    "R", "Drag anywhere to frame a region"),
    ("Window",      "window",  "W", "Hover a window, click to take it"),
    ("Full screen", "monitor", "F", "Grabs this monitor the moment you choose it"),
    ("Freeform",    "pen",     "L", "Draw a closed shape around anything"),
]

# Full screen is the only mode with nothing left to aim at, so choosing it skips
# the aim stage entirely.
IMMEDIATE_MODES = ["Full screen"]

DELAYS = ["No delay", "3s", "5s", "10s"]

# Destinations. The chooser sets the DEFAULT (the split button's face); the
# chevron always offers the other two. Keys C / S / O work directly.
# Copy is NOT the same operation for a recording — see README.
DESTINATIONS = [
    ("Copy", "copy", "C", {
        "stills": ("Copy",      "Image on the clipboard, paste anywhere.",
                   "Copied to clipboard"),
        "record": ("Copy file", "File reference — paste into a chat or folder.",
                   "File copied — paste into a chat or folder"),
    }),
    ("Save", "save", "S", {
        "stills": ("Save", "Straight to ~/Pictures/snipux.", "Saved to ~/Pictures/snipux"),
        "record": ("Save", "Straight to ~/Videos/snipux.",   "Saved to ~/Videos/snipux"),
    }),
    ("Open", "eye", "O", {
        "stills": ("Open", "Review window — annotate, crop, export.",
                   "Opening the review window"),
        "record": ("Open", "Player with trim, mute and GIF export.",
                   "Opening the player"),
    }),
]

# All three write the file first — Copy and Open included. A capture that exists
# only on a clipboard is one Ctrl+C from gone.
ALWAYS_WRITE_FILE = True

ANNOTATION_TOOLS = [
    ("pen",         "P", "Drag to draw freehand"),
    ("highlighter", "H", "Sweep over the line that matters"),
    ("arrow",       "A", "Drag from tail to head"),
    ("rect",        "R", "Drag to box something in"),
    ("step",        "S", "Click to drop the next number"),
    ("text",        "T", "Click, then type into the label"),
    ("blur",        "B", "Drag over anything private"),
    ("eraser",      "E", "Click a mark to remove it"),
]

AUDIO_SOURCES = [
    ("system", "speaker", "System", "Desktop output — what you hear"),
    ("mic",    "mic",     "Mic",    "Default input device"),
    ("off",    "mute",    "Muted",  "No audio track at all"),
]

# Stage → (label, what the user does next). The label is for your own state
# machine; the second string is the hint line under the bar.
STAGES = {
    "choose":   ("Choose",           "pick a mode, then drag a region"),
    "armed":    ("Aim",              "<mode hint from CAPTURE_MODES>"),
    "stills":   ("Mark it up",       "<tool hint from ANNOTATION_TOOLS>"),
    "recArmed": ("Ready to record",  "Reframe now — you cannot resize once it is rolling"),
    "count":    ("Counting down",    "Recording starts — Esc to stop"),
    "live":     ("Recording",        "This bar sits outside the recorded frame"),
    "done":     ("Finished",         "Trim and export in the player"),
}

SHORTCUTS = {
    "R": "mode Region", "W": "mode Window", "F": "mode Full screen", "L": "mode Freeform",
    "Space": "reopen the chooser from the armed tab",
    "Enter": "fire the stage's primary action",
    "Esc":   "close a menu, else step back / cancel",
    "C": "Copy", "S": "Save", "O": "Open",
    "Ctrl+Z": "undo", "Ctrl+Shift+Z": "redo",
}

RECORD_CONTAINER = "webm"
RECORD_FPS = 30
