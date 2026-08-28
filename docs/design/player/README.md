# Handoff: snipux recording player / trim editor — LOCKED

**Status: design locked 2026-08-27.** Colours, sizes, radii, spacing and interactions are
final. Build to them.

## Overview

The window a recording opens into — the video counterpart of the existing image review
window, and the `Open` destination for a snip made in Record mode.

It does four things and deliberately nothing else: **play it back**, **trim it**, **drop the
audio**, **export it**.

Chrome is the **review window's**, not the overlay's glass: opaque `Win` palette from the main
handoff, same 42px title bar, same dark workspace, same floating bar over the canvas, same
footer shape (status + path left, actions right). Someone who has used the image editor should
recognise this instantly — only the middle of the window is new.

Target: **Python + Qt (PySide6 / PyQt6)**.

Related, unchanged: `design_handoff_snipux/` (overlay, Settings, image review window) and
`design_handoff_snipux_flow/` (the capture flow that produces the file).

## About the design files

`reference/Snipux Player.dc.html` is **the spec** — a running prototype, not production code.
Rebuild it as Qt widgets. Open it from disk in a Chromium-based browser; it is fully
interactive: `Space` plays and the playhead really runs, drag the rail to scrub, drag the
yellow handles to trim, `I`/`O`, `←`/`→`, `M`, `L`, and every menu opens. **Where this
document is ambiguous, do what the prototype does.**

---

## Layout

```
title bar        42px   fixed
canvas           flex   video centred on the workspace + floating transport
timeline panel  ~160px  fixed — trim row + rail
footer           63px   fixed
```

Only the canvas flexes. Minimum window 980 × 640.

### Title bar

Accent square with a play glyph, then the filename, then `1017 × 562 · 30 fps · 00:27` in mono
`#6d7484`, then a dot-and-label pending indicator (`No pending edits` / **`Unsaved trim`** in
`#c8a54a`). Window buttons right; close hovers `#c0392b`.

### Canvas

Radial workspace, video framed with a 1px `#454b56` border, large soft shadow and a 7px
`rgba(255,255,255,.02)` ring — the same treatment the image editor gives a screenshot, so the
media has a visible edge.

- **Top-left badge**: `1017 × 562` then the trim state — `full length`, or `trimmed to 00:20`
  in `#c8d96a`.
- **Top-right**: zoom cluster, 60–160% in 20s.
- **Paused**: a 74px translucent play badge centres over the frame and the frame dims 28%.
  Clicking anywhere on the video toggles playback.

### Floating transport

Same shell as the annotate bar — 42px, radius 12, 28px controls, over the canvas bottom, 18px
up. Left to right:

| Control | Notes |
|---|---|
| **Play / Pause** | The only pre-lit control (`#ffffff` 12%). Glyph swaps `play` ⇄ `pause`. |
| **Prev / next frame** | Chevrons rotated ±90°. Steps `1/30 s` and pauses. |
| **Time** | `00:12.18 / 00:20` in mono — **position within the trim range over the trimmed duration**, not the source duration. What you see is what you'll get. |
| **Mute** | `speaker` ⇄ `mute`; muted turns the button red-tinted. This is an *export* decision, not a monitor volume — the tooltip says so. |
| **Loop** | Loops the trimmed range. On by default: you are judging a short clip. |
| **Speed** | `1×` is untinted; anything else tints accent, so an altered speed is visible without reading it. Menu opens upward. |

---

## The timeline

The reason the window exists, so it gets its own panel rather than a strip.

### Trim row

`TRIM` heading, then **`Start here` (I)** and **`End here` (O)** — each with a small accent
bar on the side it affects — then **`Reset`**, which greys out when nothing is trimmed.

Right-aligned, a plain-language readout, mono:

> in **00:02.18** · out **00:22.24** · keeping **00:20** of 00:27 · **−00:07 cut**

`keeping` is `#c8d96a`, the cut figure `#c8a54a`, and the cut clause appears only when
something is actually trimmed. This line is the answer to "what am I about to export".

### Rail — 96px, three stacked bands

1. **Ruler**, 16px: a tick and `mm:ss` label every 5s.
2. **Filmstrip**, 44px: 16 equal cells. Cells outside the trim range render at **38%
   opacity** — the primary way the trim reads at a glance. In the real app these are frames
   decoded at even intervals; the prototype fakes them.
3. **Waveform**, remaining ~36px: 120 bars. Inside the range `#c8d96a`, outside `#4a4f45`.
   **When muted the whole waveform greys to `#3a3f47` / `#23262d`** — so "this export has no
   audio" is visible without reading a control. No audio track at all → hide the band and
   give the filmstrip the height.

Over all three: a `rgba(10,11,13,.72)` veil outside the range, 2px accent edges at in and out,
and an inset accent ring at 18%.

### Handles and playhead

- **Handles**: 14px invisible hit area, 8 × 34 accent bar with a 2 × 14 dark grip line,
  `ew-resize`. Minimum range 0.5s; each handle pushes the other rather than crossing it.
  Dragging a handle also moves the playhead to it, so you always see the frame you're cutting on.
- **Playhead**: 2px `#ff5a52` full-height, with a time flag hanging off the top. Red means
  "now" here and "recording" in the capture bar — the same meaning, no other use in the product.
- Clicking or dragging anywhere on the rail scrubs and pauses.

**Implementation note that cost a bug:** measure the pointer fraction against the **rail**
element, held in a stable reference — never against `event.target`. Deriving it from the
pressed widget makes every handle drag resolve to 0 or 1, because the handle is 14px wide.
In Qt, keep the rail widget and use `mapFromGlobal` on it in all three handlers.

---

## Footer

Left, stacked: status (`✓ Saved` in `#9ec46a`, or **`Edited — not exported`** in `#c8a54a`)
over the full path in mono, ellipsised.

Right: **`Copy file`**, **`Show in Folder`**, and a split **`Export MP4 ▾`** as the accent
primary.

**Export is primary here, unlike the image editor where Copy is.** Trimming re-encodes, so a
file must be written; a clipboard-only result would be a lie. `Copy file` stays secondary and
copies a **file reference** — pastes into a chat, an upload field or a file manager, and does
nothing in an image editor. Same rule as the capture flow.

Export menu (`EXPORT_FORMATS`), each row with a one-line consequence and a **live size
estimate for the trimmed range**:

- **WebM** — what was recorded; no re-encode when untrimmed.
- **MP4 (H.264)** — plays anywhere.
- **GIF** — silent, loops, big above ~10s.
- **Current frame as PNG** — how you get a still out of a recording without leaving the window.

Then, permanently at the foot of the menu: *"Trimming re-encodes. The untrimmed original stays
at its own path until you overwrite it."* Never destroy the source.

---

## State model

```
playing  : bool
t        : float   # playhead, seconds
in_p     : float   # trim start
out_p    : float   # trim end, ≥ in_p + 0.5
muted    : bool    # drops the audio TRACK on export
loop     : bool    # default True
speed    : str     # SPEEDS
zoom     : int     # 60–160
fmt      : str     # EXPORT_FORMATS id
menu     : 'speed'|'export'|None
saved    : bool    # False once any edit is made
```

Playback advances `t` by `interval × speed`; on reaching `out_p` it wraps to `in_p` when
looping, else stops there. Any trim change sets `saved = False`.

## Keyboard

`Space` play/pause · `I` start here · `O` end here · `←`/`→` frame step · `M` mute · `L` loop ·
`Esc` close a menu. Suppress while a text field has focus.

## Qt notes

- **Playback**: `QMediaPlayer` + `QVideoWidget`, or `QGraphicsVideoItem` if you want the zoom
  transform. Clamp position to `[in_p, out_p]` in the `positionChanged` handler rather than
  trusting a seek to land exactly.
- **Filmstrip**: decode N frames once on open (`QMediaPlayer` frame grabs, or ffmpeg if it is
  already a dependency) and cache the pixmaps; never decode on paint.
- **Waveform**: compute peaks once on open from the audio track; if there is no audio track,
  hide the band rather than drawing a flat line.
- **Rail**: one custom-painted widget for ruler + filmstrip + waveform + veil + playhead, with
  the two handles as child widgets so they get their own cursors and hit areas.
- **Muted** means the export drops the audio track — not `setVolume(0)`. Two different things;
  the greyed waveform is promising the former.
- **Fonts**: IBM Plex Sans for chrome, IBM Plex Mono for every timecode, size and estimate.
- **Icons**: `icons/*.svg`, 24×24, `currentColor`, stroke 1.55, drawn for a 16px box in a 28px
  button. `play` and `pause` are new in this surface's vocabulary — do not substitute
  near-enough shapes.

## Files

```
design_handoff_snipux_player/
├── README.md                       this document
├── tokens_player.py                transport + timeline metrics, colours, formats
├── icons/*.svg                     23 glyphs
└── reference/
    ├── Snipux Player.dc.html       THE SPEC — interactive
    ├── Icon.dc.html
    └── support.js
```

Window chrome, footer and title-bar values come from the main handoff's `tokens.py`
(`Win`, `WinMetric`).

## Still open

1. **Multi-segment cutting** — deleting a chunk from the middle. This design does a single
   in/out range, which covers "trim the dead air off both ends". Segments need a different
   rail (per-segment blocks, a join model, a re-order question) and are worth designing
   separately rather than bolting on.
2. **`Save As…`** is absent — Export covers it, since every export writes a new file anyway.
   Add it only if users expect the image editor's button here.
3. **Speed on export** — the transport's speed is a preview control. If it should also apply
   to the exported file, that needs saying in the export menu.
4. **Trim precision** — timecodes show frames (`00:12.18`); whether trims snap to keyframes
   (fast, imprecise) or re-encode for frame accuracy (slow, exact) is an encoder decision with
   a visible consequence.
