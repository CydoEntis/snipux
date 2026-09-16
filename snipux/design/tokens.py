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
    # Sampled from the app icon's selection marquee, so the accent and
    # the mark are the same green. The previous #e3ff4f was a full-
    # saturation yellow-green that read as neon against the dark chrome --
    # "not so vibrant" was the report, and on a switch or a Save pill it
    # pulled the eye harder than the thing it was labelling.
    #
    # INK_SWATCHES' "Acid" below deliberately keeps the old value: that
    # one is a *drawing* colour, not chrome, and changing it would repaint
    # what the pen puts on the image rather than what the interface looks
    # like.
    ACCENT          = "#a8e05f"
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

    # What to use when Plex is not installed -- which is every machine, since
    # the files are not bundled. Qt's own "general" and "fixed" families are
    # the last resort, and on Windows the fixed one is Courier New: a 1980s
    # printer face that made every path and filename in Settings hard to
    # read. These are all faces that ship with their platform, so a name is
    # only reached for where it actually exists.
    UI_FALLBACKS = (
        "Segoe UI Variable Text",   # Windows 11
        "Segoe UI",                 # Windows 10
        "SF Pro Text",              # macOS
        "Helvetica Neue",
        "Inter",
        "Cantarell",                # GNOME
        "Ubuntu",
        "DejaVu Sans",
        "Noto Sans",
    )
    MONO_FALLBACKS = (
        "Cascadia Mono",            # Windows 11, and with Windows Terminal
        "Cascadia Code",
        "Consolas",                 # Windows, back to Vista
        "SF Mono",                  # macOS
        "Menlo",
        "JetBrains Mono",
        "Fira Mono",
        "Ubuntu Mono",
        "DejaVu Sans Mono",
        "Liberation Mono",
        "Noto Sans Mono",
    )

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
    HIGHLIGHT_BAND_RADIUS = 3     # corners of a highlight snapped to text
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
# The stills bar's tools, its slots and their shortcuts live with the rest of
# the stills bar's tokens at the end of this file (`STILLS_SLOTS`, `SHAPES`,
# `REDACTIONS`, `TOOLS`, `SHORTCUTS`).

# The third field is the note under each row in the mode menu. It says
# *what gets captured*, because Window and Full screen are the two that
# read as the same thing until you have used both -- "if you're capturing a
# window, you're capturing a full screen?" One is an application's window,
# the other is a whole monitor, and only the note distinguishes them at the
# moment of choosing.
#
# Window is the exception, and says what it asks of you instead (#44). Once
# Active window joined the menu, "One application's window" described both
# rows equally well: they take the same kind of thing and differ only in
# whether you aim. So Window's note leads with the hover and the click, and
# Active window's names the window you are already in.
CAPTURE_MODES = [
    ("Region",        "crop",         "Any rectangle you drag"),
    ("Window",        "window",       "Hover a window and click it"),
    ("Full screen",   "monitor",      "The whole monitor you are on"),
    ("Active window", "windowActive", "The window you are in"),
    ("Browser",       "panel",        "Your browser page, no toolbars"),
]

# The mode that captures a browser's page area. Named because three modules
# read it -- the chooser greys the row, the overlay dispatches on it, and
# IMMEDIATE_MODES lists it.
BROWSER_MODE = "Browser"

# The mode that captures the window the user was in when the snip started.
# Named for the same three readers as BROWSER_MODE.
ACTIVE_WINDOW_MODE = "Active window"



# What the row says instead of its note when there is no browser to capture
# -- no browser open, a platform that cannot see other windows (Wayland), or
# a browser that is not Chromium-family. One string for all three: the row
# has space for a reason, not for a diagnosis, and every one of them means
# the same thing to the user.
BROWSER_UNAVAILABLE = "No browser window found"

# What the Active window row says instead of its note. Two reasons where
# BROWSER_UNAVAILABLE has one, because Wayland -- the session most Linux
# users are on -- can never name another application's window, and "No
# focused window found", said to someone looking straight at a focused
# window, reads as a bug. UNSUPPORTED is a provider that cannot answer at
# all; UNAVAILABLE is one that can but found nothing to take, because the
# desktop has focus, or snipux itself does.
ACTIVE_WINDOW_UNAVAILABLE = "No focused window found"
ACTIVE_WINDOW_UNSUPPORTED = "Not available on this desktop"

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
    # Leads with the click, as the stills note does, so both sides describe
    # the mode by what it asks of you.
    "Window": "Click one; films where it is",
    # Active window needs no entry: "The window you are in" is as true of a
    # recording, and its caveat -- the same one -- is in the pill.
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
    SCROLL_THUMB    = "#2f333b"   # scrollbar handle, idle -- same weight as a control border
    SCROLL_THUMB_HOVER = "#3a3f49"
    SCROLL_THUMB_ACTIVE = "#454b56"
    TOGGLE_KNOB     = "#14161a"   # the window's own dark, so the knob reads
                                  # as a hole punched through the switch
                                  # rather than a second colour laid on it.
                                  # Off is still legible: TOGGLE_OFF above is
                                  # a lighter grey than this.

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

    # EntryList, the Settings list control: an entry per row, an add field
    # beneath. A row is the design's 8px inset row and a step shorter than a
    # field, so a list of them reads as contents rather than as more inputs.
    ENTRY_ROW_H      = 32
    ENTRY_ROW_RADIUS = 8
    ENTRY_ROW_GAP    = 6          # between rows, and above the add field
    ENTRY_TEXT_INSET = 11         # lines an entry up with a field's own text
    ENTRY_GAP        = 8          # a field and the button beside it
    ENTRY_REMOVE     = 24         # the remove control, square
    ENTRY_REMOVE_ICON = 12
    ENTRY_REMOVE_RADIUS = 6
    ENTRY_REMOVE_INSET = 4        # keeps it off the row's right edge
    ENTRY_REASON_GAP = 4          # a row and the reason beneath it
    HIDE_TOGGLE_H    = 26         # Edit as text: small enough to sit beside a heading
    HIDE_TEXT_H      = 96         # its box: a handful of lines before it scrolls

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
    ("watermark", "layers", "Watermark"),
    ("hide",    "blur",   "Hide sensitive"),
    ("tray",    "panel",  "Tray & startup"),
]

# The three kinds of entry in the user's own hide list, as the Settings page
# presents them: (section, title, what it does). The note is the whole
# explanation a user gets, so it says what the entry *does* rather than what
# it is called.
HIDE_LIST_FIELDS = (
    (
        "words",
        "Words to hide",
        "Hidden wherever they appear -- your address, your employer, a "
        "project codename. One per line.",
    ),
    (
        "labels",
        "Field names",
        "The value next to one of these is hidden, the way Password already "
        "is: on the same line, or in the box beside or beneath it.",
    ),
    (
        "patterns",
        "Patterns",
        "Regular expressions, if you want them: ACME-\\d{6} hides any "
        "ACME- badge number.",
    ),
)

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
    ("save", "Capture and save",
     "The same frozen frame and tools as above, with Save as the button "
     "already under the cursor instead of Copy."),
    ("review", "Capture and review",
     "Opens the review window afterwards, which annotates too."),
]

# `save` is the odd one, and it is here because the split action assumed it
# all along: `OverlayWindow._sync_bar_destination` has always mapped it to a
# `Save` face, and `FloatingBar`'s caret has always offered Save as one of
# its three. Until now there was no stills value to record that choice in --
# the vocabulary ran instant/edit/review, `save` was record-only, and
# picking Save from the caret therefore could not be remembered the way
# Copy and Open could. It differs from `edit` in exactly one respect, the
# face, which is the whole of what "the chooser sets the split button's
# face" means.

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
    "save": "Annotate in place, then save the file.",
    "review": "Opens the review window to edit.",
}


# Hide sensitive's tooltip and hint: black out sensitive text in every
# screenshot.
# The hint names what gets covered rather than saying "sensitive info",
# because someone deciding whether to trust it needs to know what it looks
# for -- and, by omission, what it does not.
HIDE_SENSITIVE_HINT = {
    True: "Blacks out passwords, keys, cards and personal info -- click to turn off",
    False: "Black out passwords, keys, cards and personal info",
}

# Mode -> what the user does next once it is chosen: the hint pill under the
# chooser row. There is no primary button at the end of the row, because
# nothing there could perform what one would promise.
MODE_NEXT_STEP = {
    "Region":        "Drag anywhere to frame a region",
    "Window":        "Hover a window, click to take it",
    "Full screen":   "Grabs this monitor the moment you choose it",
    "Active window": "Grabs your focused window the moment you choose it",
    "Browser":       "Grabs your browser's page the moment you choose it",
    # LAST_REGION_MODE, below. Seen when the row is reopened over the
    # region it restored, or opens on it.
    "Last region":   "Your last capture's rectangle -- drag to frame another",
}

# Mode shortcuts. Live whenever the chooser is on screen, armed or not.
# The next-step hint, where the record side needs a different one. The pill
# has room the menu row does not, so the caveat the note only gestures at
# gets said properly here.
RECORD_MODE_NEXT_STEP = {
    "Window": "Click a window to frame it -- moving it later will not follow",
    "Active window": "Frames the window you are in -- moving it later will not follow",
}

# The next-step hint on a desk with more than one monitor, for the
# MONITOR_MODES that arm there instead of firing.
MULTI_MONITOR_NEXT_STEP = {
    "Full screen": "Hover a monitor, click to take it",
}

MODE_KEYS = {
    "R": "Region", "W": "Window", "F": "Full screen", "B": "Browser",
    # A is also Arrow on the stills bar, and the two are never live at the
    # same moment: the chooser takes keys only while nothing is selected,
    # and the bar exists only once something is. That split already lets R
    # be Region here and Rectangle there.
    "A": "Active window",
}

# Modes with nothing left to aim at, so choosing one fires the grab
# immediately (after any delay). Region and Window arm and wait.
# On the record side nothing fires immediately -- see RECORD_DISABLED_MODES.
IMMEDIATE_MODES = ["Full screen", BROWSER_MODE, ACTIVE_WINDOW_MODE]

# The immediate modes that aim at a monitor. "Nothing left to aim at" holds
# for them only on a desk with one: with more, which monitor is still a
# choice, so they arm and follow the pointer instead
# (docs/design/bars/divergences.md 3).
MONITOR_MODES = ["Full screen"]

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
#     is `TOOLS` + `SHORTCUTS` + `TOOL_HINTS`, for more than eight -- see
#     `docs/design/flow/divergences.md` §7 for why the extra tools outrank
#     the handoff's own "eight tools" rule. Adopting its list would drop them.
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

# A mode the recorder cannot take stays in the mode list, greyed out, with
# the value naming why rather than being hidden (handoff for this ticket).
#
# Window used to sit here, reading "Not offered for recording yet",
# which was the honest reason: nobody had asked for it, not that anything
# stopped it. Window mode already resolves to a rect
# (`_confirm_window_pick`), and a rect is exactly what the recorder takes,
# so it needed no new machinery -- only asking for. Note the recorder films
# a fixed rectangle, so a window moved or resized mid-recording keeps
# filming the rectangle it started in.
#
# Active window is left off this list on purpose, for the same reason. It
# resolves to a rect before anything is filmed, and from there its record
# path is Window's, with nothing new downstream: the rect lands on the ready
# stage, framed on the frozen frame and still reframeable, and nothing is
# filmed until Record is pressed. A wrong rect is seen before it is
# recorded. Window's caveat applies to it too, and its pill says so.
# Full screen has nothing left to aim at on the stills side, but on the
# record side there is equally nothing *downstream* wired up yet, so it must
# not fire immediately there -- it arms and waits like Region does.
RECORD_DISABLED_MODES = {
    # Recording a browser page is a perfectly sensible thing to want, and
    # the rect is the same one. It is off here only because nothing has
    # driven it end to end on the record side yet -- offering it untested
    # would be a guess, not a feature.
    # Recording a browser page is sensible and the rect is the same one;
    # it is off because nothing has driven it end to end there. Recording
    # a page that scrolls itself is a different feature entirely, not this
    # one with a video codec on the end.
    "Browser": "Screenshots only",
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
    "pixelate":    "Drag over anything private",
    "blackout":    "Drag to black it out completely",
    "eraser":      "Click a mark to remove it",
    "ellipse":     "Drag to draw an oval",
    "line":        "Drag for a straight line",
    "callout":     "Drag from the thing it points to, then type",
}

# Fill and line style for the shape marks, from the locked stills-bar handoff:
# FILL_CYCLE, DASH_CYCLE and FILL_OPACITY in docs/design/bars/tokens_bars.py.
# Each cycle is in the order its style-popover button advances through.
FILL_CYCLE = [("outline", "Outline only"), ("filled", "Filled"), ("both", "Outline and filled")]

# The handoff writes each pattern as an SVG dash array string ("9 7", "none").
# Here it is the numbers, (on, off), the way ANTS_DASH is, and () for solid.
# They are pixels, not the multiples of the pen width QPen counts a pattern
# in -- shapes._line_pen converts.
DASH_CYCLE = [("solid", (), "Solid"), ("dashed", (9, 7), "Dashed"), ("dotted", (2, 5), "Dotted")]

# Of the STROKE colour, so a filled shape has no second colour to reconcile.
FILL_OPACITY = {"outline": 0.0, "filled": 0.90, "both": 0.22}

# The highlighter's own click-through: fit the sweep to the lines of text
# under it, or leave it as drawn. Not in the handoff -- bars/divergences.md 24.
SNAP_CYCLE = [("text", "Snap to text"), ("free", "Freehand")]


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
    """Only what differs from Color.

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

    ACCENT               = "#a8e05f"
    ACCENT_FG            = "#15170e"
    # The 1px line between a split button's face and its caret. Which half
    # a click lands in has to be visible before the click, or a split
    # button is just a button that sometimes does something else.
    SPLIT_SEAM           = "#15170e"
    SPLIT_SEAM_ALPHA     = 0.22
    ACCENT_SOFT          = "#c3e399"   # accent as TEXT or a small glyph
    # The handoff gives this as "14-18% for an armed segment"; the prototype
    # spends the range on two different things, so it is two tokens here
    # rather than one that has to be right twice. .18 is the armed kind
    # segment; PAUSE_WASH is the paused button in the live bar.
    ACCENT_WASH          = "#a8e05f"
    ACCENT_WASH_ALPHA    = 0.18
    PAUSE_WASH           = "#a8e05f"
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
    WINDOW_HOVER         = "#a8e05f"
    WINDOW_HOVER_ALPHA   = 0.85        # the 2px border
    WINDOW_HOVER_FILL    = "#a8e05f"
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

    TRIM             = "#a8e05f"        # range edges + both handles
    TRIM_INNER       = "#a8e05f"        # at 18%, inset ring
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
    ACCENT_ON_BG     = "#a8e05f"        # at 15%, loop/speed when engaged
    ACCENT_ON_FG     = "#c3e399"
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


# ---------------------------------------------------------------------------
# The stills bar and the chooser row (docs/design/bars, LOCKED 2026-09-15)
# ---------------------------------------------------------------------------
# Ported from docs/design/bars/tokens_bars.py -- its `BarMetric`, `BarColor`
# and stills-bar structures, only as far as the chooser row and the one row
# under a selection read them. docs/design/bars/divergences.md overrides that file wherever
# the two differ, and the values below already follow it.
#
# A few figures the handoff's spec (reference/Snipux Handoff Preview.dc.html)
# writes into its markup rather than into tokens_bars.py -- the split
# action's glyph, the menu rows' gaps and type, the notch's colours, the
# style dot's scale -- are here too, each marked as coming from there, so the
# bar has no literal of its own.

class BarMetric:
    """The stills bar and its menus. Logical pixels."""

    ROW_H            = 42          # 6 pad + 28 control + 6 pad + 2x1px border
    PAD              = 6
    BORDER           = 1
    GAP              = 3
    RADIUS           = 12
    RADIUS_DOCKED    = (0, 0, 12, 12)   # tl, tr, br, bl: the chooser, flush to the monitor top
    BTN              = 28
    BTN_RADIUS       = 8
    ICON             = 15
    CHEVRON          = 12
    DIVIDER_H        = 20
    DIVIDER_MARGIN   = 4

    NOTCH            = 12          # corner hit area, past the spec's box (#77)
    NOTCH_BOX        = 9           # spec markup: the box its triangle sits in
    NOTCH_TRIANGLE   = 5           # visible triangle leg
    NOTCH_INSET      = 1           # spec markup: right:1px; bottom:1px

    SPLIT_PAD_H      = 10
    SPLIT_CARET_W    = 18
    SPLIT_ICON       = 14          # spec markup
    SPLIT_GAP        = 6           # spec markup: glyph to label

    MENU_PAD         = 4
    MENU_RADIUS      = 11
    MENU_OFFSET      = 6
    MENU_ROW_PAD     = (7, 8)
    MENU_ROW_RADIUS  = 7
    MENU_ROW_GAP     = 9           # spec markup
    MENU_ROW_ICON    = 15          # spec markup
    MENU_TICK        = 13          # spec markup
    MENU_NOTE_GAP    = 2           # spec markup: label to note
    MENU_W_SHAPES    = 186
    MENU_W_REDACT    = 244         # widest: rows carry a security note
    MENU_W_STYLE     = 264         # the handoff's 216 read as squished (divergences.md 21)

    # The style popover's two rows. The handoff's structure, at roomier sizes
    # than its markup (divergences.md 21): built to 216px with 21px swatches
    # 4px apart and 30x24 buttons, it read as squished. Border-box, like
    # every menu: MENU_W_STYLE is the outside edge, padding and border
    # included.
    STYLE_PAD_V      = 12
    STYLE_PAD_H      = 12
    STYLE_ROW_GAP    = 12          # colour row to controls row
    STYLE_CONTROL_GAP = 8          # between the controls in the second row
    SWATCH_H         = 24          # seven swatches and `+`, sharing the row's width
    SWATCH_GAP       = 6
    SWATCH_RADIUS    = 7
    SWATCH_RING      = 1.5         # the light ring round the picked swatch
    SWATCH_RING_GAP  = 2           # the dark gap between that ring and the colour
    CUSTOM_ICON      = 12
    CYCLE_W          = 36          # fill and line: click-through buttons
    CYCLE_H          = 28
    CYCLE_RADIUS     = 8
    FILL_GLYPH_W     = 20
    FILL_GLYPH_H     = 13
    FILL_GLYPH_RADIUS = 2
    FILL_GLYPH_BORDER = 1.5
    DASH_GLYPH_W     = 22          # a 24px line, 1px in from each end
    DASH_GLYPH_STROKE = 2
    SNAP_GLYPH_W     = 20          # snap to text: two lines of "type"
    SNAP_GLYPH_LINE_GAP = 7        # between the two lines' centres
    SNAP_GLYPH_BAND_H = 7          # the highlight washed over the top line
    SLIDER_TRACK     = 4
    SLIDER_THUMB     = 15
    READOUT_W_SIZE   = 32          # "26px" without the row reflowing
    READOUT_W_STRENGTH = 20        # "20"

    # The style dot's diameter is the stroke at this scale, clamped -- spec
    # markup. The highlighter's own stroke paints wider, so its dot does too.
    STYLE_DOT_SCALE  = 1.6
    STYLE_DOT_SCALE_HIGHLIGHTER = 2.4
    STYLE_DOT_MIN    = 6
    STYLE_DOT_MAX    = 20
    STYLE_DOT_RING   = 1
    # An outline-only shape's dot is a ring this wide, inside its diameter.
    STYLE_DOT_OUTLINE = 2

    # The chooser row (#66).
    WELL_PAD         = 2           # recessed group: the kind pair, the flag pair
    WELL_GAP         = 2           # spec markup
    WELL_RADIUS      = 9
    WELL_BTN_RADIUS  = 7

    # The handoff's measurement of the chooser with Region chosen, in IBM
    # Plex Sans. Nothing is sized to it: the row is the metrics below plus
    # its label, which the spec's own markup sums to 246 + label -- about
    # 285 in Plex. See divergences.md, "The chooser is not 382px wide".
    CHOOSER_W        = 382

    CHIP_PAD_L       = 9           # spec markup: the mode chip, the row's one label
    CHIP_PAD_R       = 7
    CHIP_GAP         = 6
    CHIP_ICON        = 14
    FLAG_PAD_H       = 8           # spec markup: Delay's horizontal padding
    FLAG_GAP         = 5           # spec markup: Delay's glyph to its value
    MENU_RULE_MARGIN = 4           # spec markup: the rule above Last region
    MENU_W_MODE      = 240

    # The chooser's tab, once a selection exists.
    TAB_H            = 22
    TAB_PAD_H        = 11
    TAB_GAP          = 8           # spec markup
    TAB_ICON         = 12          # spec markup
    TAB_ICON_GAP     = 6           # spec markup
    TAB_SEP_H        = 11          # spec markup
    TAB_RADIUS       = (0, 0, 10, 10)
    TAB_OPACITY      = 0.70        # 1.0 on hover

    # The hint pill under the chooser row.
    HINT_GAP         = 7
    HINT_PAD         = (4, 10)     # v, h
    HINT_RADIUS      = 7
    HINT_ICON        = 12
    HINT_ICON_GAP    = 6           # spec markup

    # The spec's box-shadow is 0 18px 40px -14px. Qt's drop shadow has no
    # spread, so the offset gives up the 14px the spread would have pulled
    # it in by, or the shadow would sit well below the row.
    SHADOW_BLUR      = 40
    SHADOW_DY        = 4

    # The glass under every bar and menu: the frame behind it blurred by
    # this radius (snipux/glass.py). The bars handoff names its
    # backdrop-filter without a radius; 16 is the flow handoff's.
    BACKDROP_BLUR    = 16

    # Placement: centred on the selection, BAR_OFFSET_Y below it, and at
    # least BAR_EDGE_MARGIN inside the selection's monitor. The handoff's
    # BAR_BOTTOM_ROOM clamp is deliberately absent -- see divergences.md 8.
    BAR_OFFSET_Y     = 16
    BAR_EDGE_MARGIN  = 12


class BarFont:
    """(px, weight). tokens_bars.py has no type scale; these are the spec's
    markup."""

    SPLIT            = (12.0, 600)
    MENU_LABEL       = (12.0, 500)
    MENU_NOTE        = (10.5, 400)
    MENU_SHORTCUT    = (10.0, 400)  # mono
    READOUT          = (11.0, 500)  # mono: "5px", "8"
    # The chooser row.
    CHIP             = (12.0, 500)
    DELAY            = (11.0, 500)  # mono
    HINT             = (11.0, 400)
    TAB_MODE         = (11.0, 500)
    TAB_TAIL         = (11.0, 400)


class BarColor:
    """Alphas ride as `<TOKEN>_ALPHA` siblings, so `design.bar_color()` hands
    back one fully-specified QColor, the pairing rule every other palette in
    this file follows.
    """

    BAR_BG               = "#1a1c18"
    BAR_BG_ALPHA         = 0.94
    BAR_BORDER           = "#ffffff"
    BAR_BORDER_ALPHA     = 0.10
    DIVIDER              = "#ffffff"
    DIVIDER_ALPHA        = 0.12
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
    SHORTCUT_FG          = "#6f766a"

    TOOL_ACTIVE_BG       = "#ffffff"
    TOOL_ACTIVE_BG_ALPHA = 0.16
    TOOL_ACTIVE_FG       = "#f8faf0"
    TOOL_IDLE_FG         = "#a8afa0"
    TOOL_HOVER_BG        = "#ffffff"   # spec markup: every slot's hover wash
    TOOL_HOVER_BG_ALPHA  = 0.09
    TOOL_DISABLED_FG     = "#5d6157"   # undo, empty stack
    DANGER_BG            = "#c85050"
    DANGER_BG_ALPHA      = 0.22
    DANGER_FG            = "#f5a3a3"

    # Not the handoff's #e3ff4f. `Color.ACCENT` says why: the full-saturation
    # yellow-green read as neon on the dark chrome and was reported as such,
    # and the split action is the one place the bar spends an accent.
    ACCENT               = "#a8e05f"
    ACCENT_FG            = "#15170e"
    SPLIT_SEAM           = "#15170e"
    SPLIT_SEAM_ALPHA     = 0.22
    ACCENT_SOFT          = "#c3e399"   # accent as text or a small glyph
    ACCENT_WASH          = "#a8e05f"   # an armed flag's fill
    ACCENT_WASH_ALPHA    = 0.18

    # Spec markup: the notch takes its slot's glyph colour, faded, so it
    # lights with the slot rather than competing with it.
    NOTCH_ACTIVE         = "#f8faf0"
    NOTCH_ACTIVE_ALPHA   = 0.70
    NOTCH_IDLE           = "#a8afa0"
    NOTCH_IDLE_ALPHA     = 0.55

    STYLE_DOT_BG         = "#ffffff"
    STYLE_DOT_BG_ALPHA   = 0.06
    STYLE_DOT_BG_OPEN    = "#ffffff"   # while what it opens is open
    STYLE_DOT_BG_OPEN_ALPHA = 0.14
    STYLE_DOT_RING       = "#ffffff"   # spec markup
    STYLE_DOT_RING_ALPHA = 0.22
    DISABLED_OPACITY     = 0.34        # the style dot, for a tool with nothing to style

    # The style popover -- spec markup.
    SWATCH_BORDER        = "#ffffff"
    SWATCH_BORDER_ALPHA  = 0.20
    SWATCH_RING          = "#f1f3e8"   # the picked swatch's light ring
    SWATCH_RING_GAP      = "#1a1c18"   # and the dark gap inside it
    CUSTOM_BORDER        = "#ffffff"   # dashed
    CUSTOM_BORDER_ALPHA  = 0.32
    CUSTOM_FG            = "#a8afa0"
    CYCLE_BG             = "#000000"
    CYCLE_BG_ALPHA       = 0.34
    CYCLE_HOVER_BG       = "#ffffff"
    CYCLE_HOVER_BG_ALPHA = 0.10
    # The glyph a fill or line button draws its state in. The spec's is its
    # accent as a glyph, #eaff7a; this is ours, `FlowColor.ACCENT_SOFT`, for
    # the reason ACCENT above gives.
    CYCLE_GLYPH          = "#c3e399"
    CYCLE_GLYPH_WASH     = "#c3e399"   # "Outline and filled": the glyph's fill
    CYCLE_GLYPH_WASH_ALPHA = 0.30
    SLIDER_TRACK         = "#ffffff"
    SLIDER_TRACK_ALPHA   = 0.20
    SLIDER_THUMB         = "#f1f3e8"
    READOUT_FG           = "#c6cab8"

    # The chooser row (#66) -- spec markup where tokens_bars.py is silent.
    WELL_BG              = "#000000"
    WELL_BG_ALPHA        = 0.34
    MENU_RULE            = "#ffffff"
    MENU_RULE_ALPHA      = 0.10
    FLAG_OFF_FG          = "#8f9689"   # an unlit flag inside a well
    CONTROL_HOVER_BG     = "#ffffff"   # the destination and the flags
    CONTROL_HOVER_BG_ALPHA = 0.07
    CONTROL_HOVER_FG     = "#dfe4ec"
    CHIP_FG              = "#f1f3e8"
    CHIP_BORDER_OPEN     = "#ffffff"
    CHIP_BORDER_OPEN_ALPHA = 0.20
    # The kind pair. Record lights red -- the one place red means "this
    # films" -- and stills lights neutral.
    KIND_ON_BG           = "#ffffff"
    KIND_ON_BG_ALPHA     = 0.14
    KIND_ON_FG           = "#f1f3e8"
    KIND_HOVER_BG        = "#ffffff"
    KIND_HOVER_BG_ALPHA  = 0.08
    REC_ON_BG            = "#ff5a52"
    REC_ON_BG_ALPHA      = 0.20
    REC_ON_FG            = "#ff8d86"
    HINT_BG              = "#101210"
    HINT_BG_ALPHA        = 0.78
    HINT_BORDER          = "#ffffff"
    HINT_BORDER_ALPHA    = 0.07
    HINT_FG              = "#7d8478"
    TAB_BG               = "#1a1c18"
    TAB_BG_ALPHA         = 0.88
    TAB_SEP              = "#ffffff"
    TAB_SEP_ALPHA        = 0.16
    SHADOW               = "#000000"   # the row's drop shadow
    SHADOW_ALPHA         = 0.90
    # Where no blur can be had under the glass, a fill thinner than this
    # rises to it and nothing else changes (README, "Qt notes").
    FALLBACK_BG_ALPHA    = 0.97


# The bar's slots, left to right. The order is a gradient of consequence --
# draw on top, frame, add content, destroy pixels, remove marks -- and is
# never sorted by use or reordered at runtime: muscle memory is the feature.
# `shapes` and `redact` are families, and show whichever sibling was used
# last.
STILLS_SLOTS = ["pen", "highlighter", "shapes", "step", "text", "redact", "eraser"]

# (tool, label, shortcut): the handoff's four, plus Callout
# (docs/design/bars/README.md:78: "Rounded-rect, polygon, callout,
# spotlight -> shape siblings"). Crop was a fifth until it turned out to
# draw a dashed box and crop nothing (divergences.md 7).
SHAPES = [
    ("rect",    "Rectangle",     "R"),
    ("ellipse", "Ellipse",       "O"),
    ("line",    "Straight line", "L"),
    ("arrow",   "Arrow",         "A"),
    ("callout", "Callout",       "C"),
]

# (tool, glyph, label, note). The note is not decoration: blur on small text
# is famously recoverable, so each row has to say what it actually
# guarantees.
REDACTIONS = [
    ("blur",     "blur",     "Blur",     "Softens it — shapes still readable"),
    ("pixelate", "mask",     "Pixelate", "Blocky, obviously deliberate"),
    ("blackout", "blackout", "Blackout", "Solid bar. Nothing to reconstruct"),
]
BLACKOUT_FILL = "#0b0c09"

FAMILIES = {
    "shapes": [tool for tool, _label, _key in SHAPES],
    "redact": [tool for tool, _glyph, _label, _note in REDACTIONS],
}

# Every tool the bar can arm, in bar order.
TOOLS = [
    tool
    for slot in STILLS_SLOTS
    for tool in FAMILIES.get(slot, [slot])
]

# A tool is drawn with the glyph of the same name, except where the handoff
# gave it another.
TOOL_GLYPHS = {tool: glyph for tool, glyph, _label, _note in REDACTIONS}

# The spec's names, which tooltips use.
TOOL_NAMES = {
    "pen": "Pen", "highlighter": "Highlighter", "rect": "Rectangle",
    "ellipse": "Ellipse", "line": "Straight line", "arrow": "Arrow",
    "step": "Numbered step", "text": "Text", "callout": "Callout",
    "blur": "Blur", "pixelate": "Pixelate", "blackout": "Blackout",
    "eraser": "Eraser",
}

# One letter, one tool. Every shape sibling keeps its own letter, so its menu
# is for discovery rather than for use.
SHORTCUTS = {
    "P": "pen", "H": "highlighter",
    **{key: tool for tool, _label, key in SHAPES if key},
    "S": "step", "T": "text", "E": "eraser",
}

# The redaction family has one key between three siblings, and it cycles.
REDACTION_KEY = "B"

# Which style popover sections a tool shows, in the order they are laid out.
# Anything absent is not rendered -- never rendered-but-inert.
STYLE_SECTIONS = {
    "pen":         ["color", "size"],
    "highlighter": ["color", "snap", "size"],
    "rect":        ["color", "fill", "dash", "size"],
    "ellipse":     ["color", "fill", "dash", "size"],
    "line":        ["color", "dash", "size"],
    "arrow":       ["color", "dash", "size"],
    "step":        ["color", "size"],
    "text":        ["color", "size"],
    "callout":     ["color", "fill", "dash", "size"],
    "blur":        ["strength"],
    "pixelate":    ["strength"],
    "blackout":    [],
    "eraser":      [],
}

# Nothing on the style dot can change what these draw, so it dims and does
# not open for them.
UNSTYLED_TOOLS = [tool for tool, sections in STYLE_SECTIONS.items() if not sections]

# Style is per tool and remembered for the session, so a dashed red box never
# turns the pen red. The handoff's first-run seed, as it wrote it.
DEFAULT_STYLE = {
    "pen":         {"color": "#e3ff4f", "size": 5, "dash": "solid", "fill": "outline"},
    # Yellow, not the handoff's amber: a highlighter is expected to be yellow,
    # and amber at HIGHLIGHT_ALPHA read as orange (bars/divergences.md 24).
    "highlighter": {"color": "#facc15", "size": 5, "dash": "solid", "fill": "outline",
                    "snap": "text"},
    "rect":        {"color": "#ef4444", "size": 3, "dash": "dashed", "fill": "both"},
    "ellipse":     {"color": "#38bdf8", "size": 3, "dash": "solid", "fill": "outline"},
    "line":        {"color": "#e3ff4f", "size": 3, "dash": "solid", "fill": "outline"},
    "arrow":       {"color": "#ef4444", "size": 3, "dash": "solid", "fill": "outline"},
    "step":        {"color": "#ef4444", "size": 5, "dash": "solid", "fill": "filled"},
    "text":        {"color": "#ffffff", "size": 5, "dash": "solid", "fill": "outline"},
    "callout":     {"color": "#ef4444", "size": 3, "dash": "solid", "fill": "outline"},
}
# What the spec seeds every tool DEFAULT_STYLE leaves out with.
DEFAULT_STYLE_OTHER = {"color": "#e3ff4f", "size": 5, "dash": "solid", "fill": "outline"}

STROKE_RANGE   = (1, 26)
STRENGTH_RANGE = (2, 20)


# ---------------------------------------------------------------- the chooser
# Last region is a MODE, not a flag: it answers "what to capture". It sits
# under a rule at the foot of the mode menu, subtitled with the dimensions it
# restores. It was once a labelled toggle on the row, labelled because "the
# icon makes no sense, i had to hover to tell what is was doing"; a menu row
# carrying its name and its dimensions answers that without a label on the row.
LAST_REGION_MODE = "Last region"
LAST_REGION_GLYPH = "undo"
LAST_REGION_SHORTCUT = "Shift+R"
LAST_REGION_NOTE = "The rectangle your last capture came from"
# Why the row is greyed. Two, because a capture remembered on a monitor that
# is not here now is not "nothing captured".
LAST_REGION_NONE = "Nothing captured yet"
LAST_REGION_OFF_DESK = "Not on these monitors"

HIDE_SENSITIVE_GLYPH = "eyeOff"

# Tooltips. Qt's tooltips are unreliable on an always-on-top frameless
# window, so the row also borrows the hint pill for these while hovered.
MODE_CHIP_TOOLTIP = "What to capture"
KIND_TOOLTIP = {"stills": "Still image", "record": "Screen recording"}
DESTINATION_TOOLTIP = "After capture: {label} -- {note}"
DELAY_TOOLTIP_OFF = "No delay -- click to add one"
DELAY_TOOLTIP_ON = "{delay} countdown before the grab -- click for the next"
TAB_TOOLTIP = "Reopen the chooser -- Space"


# ---------------------------------------------------------------------------
# The watermark (#69)
# ---------------------------------------------------------------------------
# The stills bar's watermark slot, its menu, the mark it stamps, and the
# Settings page that says what the mark is. The slot and the menu are
# docs/design/bars/tokens_bars.py's `WATERMARK` and
# `BarMetric.MENU_W_WATERMARK`, with the figures the spec writes into its
# markup rather than its tokens (reference/Snipux Handoff Preview.dc.html).
# What the mark is -- text or an image -- is divergences.md 10. The handoff
# leaves the mark to Settings and draws only a placeholder, so how it is
# sized and coloured is decided here, and each figure says why.

# As tokens_bars.py has it. `inset` is logical pixels, from the capture's
# edge to the mark's; the opacities are percent.
WATERMARK = {
    "glyph": "layers",
    "corners": ["tl", "tr", "bl", "br"],
    "default_corner": "br",
    "inset": 14,
    "opacity_range": (20, 100),
    "default_opacity": 70,
    "content_lives_in": "Settings",
}

# (corner, name), in the order the menu's two-by-two grid reads them.
WATERMARK_CORNERS = [
    ("tl", "Top left"), ("tr", "Top right"),
    ("bl", "Bottom left"), ("br", "Bottom right"),
]


class WatermarkMetric:
    """The slot's menu, the mark, and the Settings page. Logical pixels."""

    # The menu: spec markup, except its width.
    MENU_W           = 238         # tokens_bars.BarMetric.MENU_W_WATERMARK, border included
    MENU_PAD         = (11, 12)    # v, h
    MENU_RADIUS      = 12
    MENU_OVERHANG    = 6           # right:-6px -- its right edge, past its slot's
    MENU_SECTION_GAP = 11
    MENU_LABEL_GAP   = 6           # "Corner" to its grid
    CORNER_GAP       = 4
    CORNER_H         = 30
    CORNER_RADIUS    = 7
    CORNER_FRAME     = (26, 16)    # the capture drawn inside each corner button
    CORNER_FRAME_RADIUS = 3
    CORNER_DOT       = (7, 4)      # the mark drawn inside that capture
    CORNER_DOT_RADIUS = 1
    CORNER_DOT_INSET = 3
    OPACITY_GAP      = 9
    READOUT_MIN_W    = 30

    # The mark. Its height follows the capture, so a logo in the corner of a
    # whole monitor is not the size of one on a single button: this share of
    # the capture's shorter side, held between a floor that stays legible
    # and a ceiling that keeps it a watermark rather than a banner.
    MARK_H_SHARE     = 0.05
    MARK_H_MIN       = 20
    MARK_H_MAX       = 64
    # A wide logo is held to this share of the capture's width, so a
    # banner-shaped image cannot run the length of a short capture.
    MARK_W_SHARE     = 0.4
    # A text mark is the spec's own placeholder chip -- font 600 10.5px/1,
    # padding 5px 9px, radius 6px, letter-spacing .04em -- grown as a whole
    # to the mark's height, so at the floor it is very nearly that chip.
    TEXT_PX          = 10.5
    TEXT_PAD         = (5, 9)      # v, h
    TEXT_RADIUS      = 6
    TEXT_TRACKING    = 0.04        # em

    # The Settings page.
    THUMB_W          = 132
    THUMB_H          = 72
    THUMB_PAD        = 8
    TEXT_MAX_CHARS   = 120


class WatermarkFont:
    """(px, weight). Spec markup."""

    SECTION          = (9.5, 600)  # upper-cased
    SECTION_TRACKING = 0.10        # em
    LABEL            = (11.0, 400)
    READOUT          = (11.0, 500) # mono
    NOTE             = (10.5, 400)
    MARK_WEIGHT      = 600


class WatermarkColor:
    """The slot, its menu and the mark. Opacities ride as `<TOKEN>_ALPHA`
    siblings, and `design.watermark_color()` resolves the pair.

    The spec spends its accent on the slot while the watermark is on: its
    #e3ff4f washed at 18% under a #eaff7a glyph. Ours is `Color.ACCENT`, for
    the reason that token gives, under `FlowColor.ACCENT_SOFT`, the lighter
    accent this repo already draws small glyphs in.
    """

    ON_BG            = "#a8e05f"   # at 18%
    ON_BG_ALPHA      = 0.18
    ON_FG            = "#c3e399"
    OFF_FG           = "#8f9689"
    NOTCH_ON         = "#c3e399"   # at 70%
    NOTCH_ON_ALPHA   = 0.70
    NOTCH_OFF        = "#8f9689"   # at 55%
    NOTCH_OFF_ALPHA  = 0.55

    SECTION_FG       = "#616a5c"
    LABEL_FG         = "#8f9689"
    READOUT_FG       = "#c6cab8"
    NOTE_FG          = "#6d7484"
    CORNER_BORDER    = "#2b2f36"
    CORNER_ON_BORDER = "#a8e05f"   # at 45%
    CORNER_ON_BORDER_ALPHA = 0.45
    CORNER_ON_BG     = "#a8e05f"   # at 14%
    CORNER_ON_BG_ALPHA = 0.14
    CORNER_HOVER_BG  = "#ffffff"   # at 7%
    CORNER_HOVER_BG_ALPHA = 0.07
    CORNER_FRAME     = "#ffffff"   # at 22%
    CORNER_FRAME_ALPHA = 0.22
    CORNER_DOT       = "#8f9689"
    CORNER_DOT_ON    = "#c3e399"

    # The text mark: the spec's placeholder chip, light type on a dark
    # plate. Not a colour of the user's: a watermark lands on whatever the
    # capture holds in that corner, and the plate brings its own dark ground
    # with it, where a colour picked against one capture can vanish into the
    # next one's background.
    MARK_TEXT        = "#f1f3e8"
    MARK_PLATE       = "#0c0d0a"   # at 42%
    MARK_PLATE_ALPHA = 0.42


WATERMARK_TOOLTIP_ON = "Watermark is on — applied on export"
WATERMARK_TOOLTIP_OFF = "Watermark is off"
WATERMARK_NOTCH_TOOLTIP = "Watermark options"
WATERMARK_MENU_NOTE = "The mark itself — text or an image — is set once in Settings."

# Why the slot is greyed. It stays on the bar either way, so its tooltip
# says why instead of what it does.
WATERMARK_UNSET = "Watermark — set its text or image in Settings first"
WATERMARK_IMAGE_MISSING = "Watermark image is missing — choose it again in Settings"
WATERMARK_IMAGE_UNREADABLE = "Watermark image can't be read — choose it again in Settings"

# What a watermark can be, as the Settings page offers it: (kind, label, note).
WATERMARK_KINDS = [
    ("text", "Text",
     "A line of text, in light type on a dark plate of its own."),
    ("image", "Image",
     "A logo or any picture, drawn as it is. A PNG keeps its transparency."),
]
WATERMARK_KIND_DEFAULT = "text"
