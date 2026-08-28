"""snipux — recording player / trim editor tokens (LOCKED 2026-08-27).

The player reuses the REVIEW WINDOW's chrome, not the overlay's glass. Import
`Win` / `WinMetric` from the main handoff's tokens.py for the window shell,
title bar and footer; this file covers only what the player adds — the transport
bar and the timeline rail.

Structure, top to bottom: title bar (42) · canvas (flex) · timeline panel
(~160) · footer (~63). Only the canvas flexes.
"""

class PlayerMetric:
    WINDOW_MIN       = (980, 640)
    TITLEBAR_H       = 42
    FOOTER_H         = 63
    FOOTER_PAD       = (13, 16)

    # Floating transport, over the canvas bottom — same shell as the annotate bar
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
    WORKSPACE        = ("radial", (0.50, 0.30), 0.85, [(0.0, "#171a1f"), (1.0, "#0c0d10")])
    FRAME_BORDER     = "#454b56"

    RAIL_BG          = "#14161a"
    RAIL_BORDER      = "#262a31"
    RULER_RULE       = "#1f2229"
    TICK             = "#2f333b"
    TICK_FG          = "#5f6674"

    FILM_CELL        = "#26271f"        # the recorded content's own tone
    FILM_SEAM        = "#000000"        # at 35%
    OUTSIDE_OPACITY  = 0.38             # filmstrip cells outside the trim range
    OUTSIDE_VEIL     = "#0a0b0d"        # at 72%, over ruler-to-bottom

    WAVE_IN          = "#c8d96a"        # inside the range, audio kept
    WAVE_OUT         = "#4a4f45"        # outside the range
    WAVE_MUTED_IN    = "#3a3f47"        # muted: the whole waveform greys
    WAVE_MUTED_OUT   = "#23262d"

    TRIM             = "#e3ff4f"        # range edges + both handles
    TRIM_INNER       = "#e3ff4f"        # at 18%, inset ring
    HANDLE_GRIP      = "#15170e"        # at 50%, the 2×14 line in the handle
    PLAYHEAD         = "#ff5a52"        # red = "now", matching the recording bar
    PLAYHEAD_FG      = "#2a0d0b"        # text in the playhead's time flag

    KEPT_FG          = "#c8d96a"        # "keeping 00:20"
    CUT_FG           = "#c8a54a"        # "−00:07 cut"
    MUTED_FG         = "#f5a3a3"
    MUTED_BG         = "#c85050"        # at 20%

    SAVED_FG         = "#9ec46a"
    DIRTY_FG         = "#c8a54a"


# Playback speeds. 1× is the only one that renders without the accent tint —
# an altered speed must be visible without reading the number.
SPEEDS = ["0.5", "1", "1.5", "2"]

# Export formats. Size estimates are bytes/second × trimmed duration.
EXPORT_FORMATS = [
    ("webm",  "save",   "WebM",                  "What was recorded — no re-encode when untrimmed.", 0.42),
    ("mp4",   "save",   "MP4 (H.264)",           "Plays anywhere. Slack, Teams, browsers.",          0.55),
    ("gif",   "image",  "GIF",                   "Silent, loops. Big above ~10 seconds.",            1.90),
    ("frame", "camera", "Current frame as PNG",  "Just the frame under the playhead.",               None),
]
DEFAULT_FORMAT = "mp4"

# The footer's primary is EXPORT, not Copy: trimming re-encodes, so a file must
# be written. Copy stays secondary and copies a file REFERENCE (see main README).
FOOTER_ACTIONS = ["Copy file", "Show in Folder", "Export ▾"]

# Never destructive to the source. The untrimmed original stays at its own path
# until the user explicitly overwrites it — stated in the export menu.
PRESERVE_ORIGINAL = True

SHORTCUTS = {
    "Space": "play / pause",
    "I": "set the start at the playhead",
    "O": "set the end at the playhead",
    "Left": "previous frame",
    "Right": "next frame",
    "M": "mute (drops the audio track on export)",
    "L": "loop the trimmed range",
    "Esc": "close an open menu",
}

FPS = 30
