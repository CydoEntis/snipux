# Handoff: snipux in-place capture overlay

## Overview

A redesign of the snipux screenshot tool's annotation step. The current build opens a
separate editor window with a single congested row of text buttons, and the user cannot
tell where the screenshot ends or what they are editing.

This design removes the editor window entirely. The user drags a region, the screen
freezes, and they annotate **directly on the frozen desktop** inside the selection. The
selection stays live and resizable the whole time, so the boundary of the capture is
always visible. One floating bar sits under the selection; tool settings appear as a tray
only while a drawing tool is held.

Target implementation: **Python + Qt (PySide6 or PyQt6)**, as a frameless full-screen
overlay window.

## About the design files

The files in `reference/` are **design references written in HTML** — running prototypes
that show the intended look and behaviour. They are not production code and should not be
ported line by line. The task is to **rebuild these designs as Qt widgets** using the
project's existing patterns.

Open `reference/Snipux Overlay.dc.html` in a browser (Chromium-based; it uses
`backdrop-filter`). It is fully interactive: pick a tool and drag on the shot, drag the
selection edges, press the keyboard shortcuts, open the capture-mode chip. **Use it as the
behavioural spec** — anything ambiguous in this README, do what the prototype does.

`reference/Snipux Editor.dc.html` is the earlier exploration: two separate-editor-window
layouts (`1a`, `1b`) that were rejected in favour of the in-place model. Kept for context
only — do not build it.

## Fidelity

**High-fidelity.** Colours, type sizes, radii, spacing and interaction behaviour are final
and should be matched. Exact values are in `tokens.py`; import from it rather than
re-typing literals. Where Qt cannot reproduce a CSS effect exactly (see *Qt notes*), match
the intent, not the mechanism.

---

## Screens / views

There is **one** view: the overlay. It has no window chrome, no title bar, no menu bar.

### Overlay window

- **Purpose**: pick a region, annotate it in place, copy or save.
- **Window**: frameless, always-on-top, full screen across the active display, spanning
  the whole virtual desktop if the user has multiple monitors.
- **Background**: a still frame of the desktop grabbed the moment the overlay opens
  (`QScreen.grabWindow(0)`). Everything the user sees behind the overlay is that pixmap —
  the live desktop is not visible. This is what "frozen" means, and the `Frozen` pill in
  the top-right of the selection tells the user so.
- **Layout**: absolute positioning throughout; nothing flows. Four layers, back to front:
  1. the frozen desktop pixmap, full window
  2. the dim scrim — everything **outside** the selection, `#0c0d0a` at 62%
  3. the selection: undimmed pixmap, ink layer, frame stroke, handles, chips
  4. the floating bar, settings tray, capture-mode popover, toast

In the reference file the desktop behind the overlay is a mock (a fake editor with a file
sidebar and monospace log lines) purely so the prototype has something to annotate. **Do
not build it** — the real one is the screen grab.

#### 1. Dim scrim

Everything outside the selection rectangle. `#0c0d0a` at **62%** alpha. No blur, no
desaturation — only the flat scrim, so the surrounding context stays readable enough to
judge the crop.

Qt: paint the grab pixmap full-window, then fill `QRegion(window) - QRegion(selection)`
with the scrim colour. Do not stack a semi-transparent child widget over the whole window;
it will eat the ink layer's mouse events.

#### 2. Selection frame

- Rect kept in **window coordinates**; minimum 200 × 140.
- Two coincident 1px strokes on the rect: white at 92%, then `#1b1c16` dashed `7 7` on top,
  animated (`stroke-dashoffset` → −14 over 700ms, linear, looping) so it reads as marching
  ants. In Qt: a `QTimer` at ~30fps advancing a dash offset on a `QPen` with
  `setDashPattern([7, 7])` and `setDashOffset(n)`.
- **Corner brackets**: L shapes at each corner, 26px arms, 4px thick, solid white,
  offset −2px so they straddle the stroke. Outer corner rounded 3px.
- **Edge handles**: white rounded bars centred on each edge — 34 × 9 on top and bottom,
  9 × 34 on left and right, radius 5, offset −5px so they overhang the stroke.
- **Corner handles**: no visible chrome (the brackets read as the handle). A 20 × 20
  invisible hit target at each corner, offset −7px.
- Handle cursors: `nwse-resize` / `nesw-resize` on corners, `ns-resize` top and bottom,
  `ew-resize` left and right. Inside the selection: `crosshair` for every tool except the
  eraser, which is `pointer`.

#### 3. Chips above the selection

Both sit 38px above the selection's top edge, outside it.

- **Dimension chip** (left-aligned to the selection's left edge): light chip, `#e9ecf2`
  background, `#12141a` text, radius 8, padding 6/11, mono 12px/600. Reads
  `1040 × 560` then a `#9ca3af` middot then the mark count in 400 weight `#4b5563` —
  `2 marks`, and **`1 mark`** singular. Live during a resize drag.
- **Frozen pill** (right-aligned to the selection's right edge): `#141512` at 78%,
  `#e5e7d9` text, radius 8, 12px/400, a 13px pin icon then the word `Frozen`.

#### 4. Floating bar

Centred horizontally on the selection, 18px below its bottom edge. Its centre is clamped
to at least 400px from either screen edge, and its top is clamped to
`screen_height − 118` so it can never leave the screen when the selection is dragged low.

Chrome: `#1a1c18` at 93%, 1px `#ffffff` at 10%, radius 14, padding 7/8, 3px gap between
buttons, `backdrop-filter: blur(16px)` behind it. Drop shadow: 50px blur, 24px down,
black at 62%.

Contents, left to right:

| # | Control | Notes |
|---|---------|-------|
| 1 | **Capture-mode chip** | `#e3ff4f` fill, `#15170e` text, 12.5px/600, height 34, radius 10, padding 12 left / 9 right, label + 14px chevron. The bar's only primary action. Opens the popover. |
| 2 | divider | 1px × 22, `#ffffff` at 12%, 6px margin each side |
| 3–10 | **Tools**: pen, highlighter, arrow, rect, step, text, blur, eraser | 34px square buttons, radius 10, 18px glyph |
| 11 | divider | |
| 12–14 | **Undo, Redo, Clear ink** | undo/redo grey out to `#5d6157` when their stack is empty. Clear hovers to `#c85050` at 22% with `#f5a3a3` glyph. |
| 15 | divider | |
| 16 | **Copy** | icon only, `#d7dacb` |
| 17 | **Save** | icon + the word `Save`, `#ffffff` at 10% fill, `#f1f3e8` text, 12.5px/600, radius 10, padding 0 13 |

Button states: idle glyph `#a8afa0` on transparent; hover `#ffffff` at 9%; **active tool**
`#ffffff` at 16% with an `#f8faf0` glyph. Only one tool is active at a time. Every button
has a tooltip carrying its shortcut — `Pen — P`, `Undo — ⌘Z` (use `Ctrl` on Linux/Windows).

Eleven groups instead of the twenty-plus controls in the old bar; that reduction is the
point of the design. Do not add controls to this bar — new tools belong in the tray or a
submenu.

#### 5. Settings tray

Sits 8px **below** the bar, centred on it. Same glass treatment as the bar, radius 12,
padding 8/12, 12px gaps, 40px blur / 18px down shadow.

It is **visible only while a drawing tool is selected** (pen, highlighter, arrow, rect,
step, text) and shows a different set for blur. With the eraser selected there is no tray
at all. This conditional visibility is the core idea: colour and stroke are not controls
until the user is holding something that draws.

Draw tray, left to right:

1. Active-tool pill: `#ffffff` at 8%, radius 8, padding 3/9/3/6, 14px glyph + tool name,
   `#f1f3e8` 12px/500.
2. divider (1px × 20, `#ffffff` 12%)
3. Seven ink swatches, 22px, radius 7, 6px gaps, 1px `#ffffff` at 20% border. The selected
   one gets a double ring: `0 0 0 2px #1a1c18, 0 0 0 3.5px #f1f3e8` — i.e. a 2px dark gap
   then a 1.5px light ring. Order and hexes are in `tokens.INK_SWATCHES`.
4. A "custom colour" button: same 22px box, 1px **dashed** `#ffffff` at 32%, transparent
   fill, 12px plus glyph. Opens the platform colour picker (`QColorDialog`).
5. divider
6. Stroke slider, 104px wide, range 1–26, default 5. Track 4px, radius 99, `#ffffff` at
   20%; thumb 13px circle `#f1f3e8`.
7. Stroke readout, mono 11px/500 `#c6cab8`, minimum width 34px so the tray does not
   reflow as the number changes — `5px`.
8. Live preview dot in a 28px box: a filled circle of the current colour at the current
   stroke diameter (×3.5 for the highlighter), clamped 4–26px.
9. divider
10. Hint text, 11.5px/400 `#8f9689`, from `tokens.TOOL_HINTS`.

Blur tray: a two-segment toggle (`Blur` / `Pixelate`) in a `#000000` 35% inset well,
radius 8, active segment `#ffffff` at 16% with `#f8faf0` text; then `Strength`, a 104px
slider (2–20, default 8), a mono readout, then the hint.

#### 6. Capture-mode popover

Opens from the yellow chip. 262px wide, padding 6, radius 12, `#1a1c18` at 97%, 1px
`#ffffff` at 12%, 60px blur / 26px down shadow.

**It opens upward** — anchored above the bar — because the bar already sits near the
bottom of the screen. Rule: if bar top > 300px, place the popover at
`bar_top − popover_height − 8`; otherwise place it below the bar. It hangs over the
selection, which is correct for a transient menu.

Rows (from `tokens.CAPTURE_MODES`): 16px glyph, then a two-line label — 12.5px/500
`#a8afa0`, with an 11px/400 `#8f9689` note underneath — then a check glyph in `#e3ff4f`
at the right for the selected row. Selected row background `#ffffff` at 8% with `#f8faf0`
label; hover `#ffffff` at 9%. Row padding 8/9, radius 8.

Then a 1px `#ffffff` 10% separator with 5px/4px margins, then a **Delay** row: timer glyph,
the word `Delay`, and the current value right-aligned in mono 11.5px `#8f9689`. Clicking
cycles `Off → 3s → 5s → 10s → Off`.

#### 7. Top hint HUD

Full width, 44px tall, `#141512` at 50% with a 3px backdrop blur, contents centred,
12px/400 `#d9dbcd`. Key names are mono in pure white. Reads:

> **Esc** discard ink · **Enter** copy & dismiss · **P H A R S T B E** pick a tool · drag any edge to re-frame — the ink stays where you put it

Optional — it is behind a preference in the design (`hints`). Default on; a first-run
affordance worth hiding after N successful captures.

#### 8. Toast

Bottom centre, 34px from the bottom, above everything. `#e9ecf2` at 96%, `#12141a` text,
radius 11, padding 10/15, 12.5px/500, a 15px glyph then the message. Enters with a 180ms
ease: opacity 0→1 and 10px rise. Auto-dismisses after 2000ms; a new toast replaces the old
one and restarts the timer.

Messages and glyphs: `Copied to clipboard` (copy), `Saved to ~/Pictures/snipux` (save),
`Ink cleared` (trash), `Ink discarded` (trash, on Esc).

---

## Interactions & behaviour

### Drawing

Pointer press inside the selection starts a mark; move extends it; release commits it.

- **pen / highlighter** — accumulate points into a polyline; render as a single stroked
  path, round caps and joins. Highlighter uses stroke × 3.5 at 34% alpha.
- **arrow** — press point is the tail, cursor is the head. Head is a filled triangle:
  length `max(10, stroke × 3.4)`, half-width `max(7, stroke × 2.2)`; the shaft stops at
  55% of the head length so it does not poke through the tip.
- **rect** — press-drag rectangle, 3px corner radius, stroked, no fill. Normalise negative
  width/height on release.
- **step** — click only. Drops a 26px filled circle centred on the click, in the current
  ink colour, with `#15170e` numerals 13px/600 and a 2px white ring plus a soft drop
  shadow. Numbering is `count(existing steps) + 1`; it does **not** renumber after a
  delete (matches the prototype).
- **text** — click drops an editable label seeded with `Label`, focused for immediate
  typing. Font size `max(12, stroke × 3)`. Chrome: `#0c0e12` at 72% background, radius 5,
  padding 3/8, 1px white 16% ring, text in the ink colour.
- **blur** — press-drag rectangle, filled with a blur of the underlying pixmap.
  `Blur` = gaussian at `strength`; `Pixelate` = the same blur plus a contrast/saturation
  lift (in Qt, downsample to `1/strength` and scale back up with
  `Qt.FastTransformation`). **This must be destructive on export** — bake the effect into
  the output pixels. The tray says so, and it is a promise.
- **eraser** — no drag. Marks become hit-testable only while the eraser is active; a click
  deletes the topmost mark under the cursor. Do not implement a rubbing motion.

Marks under the minimum size are discarded on release: freehand needs > 1 point, shapes
need > 3px in either axis.

### Ink lives in screen coordinates

Store every mark in **window/screen coordinates, not selection-relative coordinates.**
This is why re-framing works: the selection can move or resize under the ink and the ink
stays over the pixels it was drawn on. The ink layer is clipped to the selection rect, so
strokes that fall outside simply stop being visible — they are not deleted, and reappear if
the user re-frames wider. The reference implements this as a full-window ink layer offset
by `-sel.x, -sel.y` inside a clipping selection; in Qt just `painter.setClipRect(sel)`
before drawing marks.

### Re-framing

Press on any handle and drag. The opposite edge stays anchored. Constraints, applied in
this order: minimum 200 × 140 (the dragged edge stops, the anchor never moves); `x ≥ 0`;
`y ≥ 52` (clear of the hint HUD); the rect stays inside the window; and
`height ≤ window_height − y − 130` so the bar always has room below.

Handle presses must not start a stroke — stop event propagation at the handle.

### Undo / redo

Two stacks of whole marks. Undo pops the newest mark to the redo stack; any new mark
clears the redo stack. Clear-ink empties both and toasts. Undo/redo buttons show a
disabled colour when their stack is empty but are not actually disabled widgets in the
reference — matching either is fine, disabled is better.

### Keyboard

| Key | Action |
|-----|--------|
| `P H A R S T B E` | select pen, highlighter, arrow, rect, step, text, blur, eraser |
| `Ctrl/⌘ Z` | undo |
| `Ctrl/⌘ Shift Z` | redo |
| `Esc` | discard all ink, toast `Ink discarded` — the prototype keeps the overlay open; in the real app decide whether Esc should also dismiss the overlay, and if so make it two-stage (ink first, then close) |
| `Enter` | copy to clipboard and dismiss |

Suppress all of these while a text label or a slider has focus.

### Capture modes

The chip is a mode selector, not an action. `Region` is the default and the only mode the
prototype simulates.

- **Region** — the drag-a-rectangle behaviour specified above.
- **Window** — hover highlights the window under the cursor (snap the selection to its
  frame); click accepts it. Then annotation proceeds identically.
- **Full screen** — selection = the whole display.
- **Freeform** — lasso; the selection becomes a path, the dim scrim inverts against it,
  and export crops to its bounding box with the outside transparent.
- **Delay** — `Off / 3s / 5s / 10s`. When set, the overlay dismisses, waits, re-grabs and
  re-opens. Show a countdown.

Modes beyond Region are specified but not prototyped — build Region first.

## State management

```
view          : QSize        # window size; recompute clamps on resize
sel           : QRect        # selection, window coords, min 200×140
tool          : str          # one of tokens.TOOLS
color         : str          # current ink hex, default #e3ff4f
size          : int          # stroke 1–26, default 5
strength      : int          # blur 2–20, default 8
blur_mode     : 'blur'|'pix'
mode          : str          # capture mode label, default 'Region'
menu_open     : bool
delay         : str          # 'Off' | '3s' | '5s' | '10s'
marks         : list[Mark]   # ordered back to front; screen coords
redo          : list[Mark]
toast         : (text, glyph) | None
```

`Mark` is a small record: `id`, `type`, `color`, `size`, `opacity`, plus geometry —
`points[]` for freehand, `x/y/w/h` for shapes and blur, `x/y/n` for a step badge,
`x/y/text/font_size` for a label.

Transitions: tool buttons and shortcut keys set `tool` and close the menu; pointer
press/move/release append and mutate the newest mark; handles mutate `sel`; copy/save/clear
set `toast` and start a 2s single-shot timer.

No data fetching. The only I/O is the screen grab on open, the clipboard on copy, and the
file write on save (`~/Pictures/snipux/`, PNG, timestamped).

## Design tokens

All of them are in **`tokens.py`** — colours with their alphas, the ink swatch list,
the type scale, every metric and radius, shadow parameters, shortcut map, capture modes,
tool hints. Import it; do not re-type values into widget code.

Two things worth restating because they are easy to get wrong:

- **Type**: IBM Plex Sans for all chrome, IBM Plex Mono for **every** numeral, dimension
  and hex readout. Ship both with the app (`QFontDatabase.addApplicationFont`) rather than
  relying on system fonts, and set fixed pixel sizes — the layout is tuned to them.
- **Alpha is not opacity**: the bar is a 93%-alpha *fill*, not a 93%-opacity widget. Its
  children are fully opaque. Painting the widget at 0.93 opacity will wash out the icons.

## Assets

- `icons/*.svg` — all 22 glyphs used by the design, 24×24, `stroke="currentColor"`,
  stroke-width 1.55, round caps and joins. Load with `QIcon`, or recolour by parsing and
  substituting `currentColor` at load time. They are drawn to sit in an 18px box inside a
  34px button.
- Fonts: IBM Plex Sans and IBM Plex Mono, SIL Open Font License, from the IBM Plex
  release. Not included here — pull the weights 400/500/600 for Sans and 400/500 for Mono.
- No raster assets. Nothing else is needed.

## Qt notes

The handful of places where CSS and Qt genuinely diverge:

- **`backdrop-filter: blur(16px)`** on the bar, tray and popover has no Qt equivalent.
  Since the desktop behind is a static pixmap, you can fake it exactly: blur the
  corresponding region of the grab once, cache it, and paint that crop as the widget's
  background before the fill. Cheaper fallback: raise the fill alpha to ~0.97 and skip the
  blur. Do not use a live blur — it will cost frames during drawing.
- **`box-shadow`** → `QGraphicsDropShadowEffect` on frameless popup widgets, or paint a
  blurred rounded rect. Parameters in `tokens.Shadow`.
- **Marching ants** → dashed `QPen` + `setDashOffset` on a timer, as above.
- **Hover states** → `enterEvent`/`leaveEvent` (or a QSS `:hover` rule) on custom
  buttons; nothing here needs a stylesheet beyond that.
- **Structure** → one frameless top-level `QWidget` (`Qt.FramelessWindowHint |
  Qt.WindowStaysOnTopHint`, `Qt.WA_TranslucentBackground`) with a custom `paintEvent`
  for pixmap + scrim + selection + ink, plus real child widgets for the bar, tray,
  popover and toast so their buttons, sliders and tooltips come for free. Do not build
  the bar inside `paintEvent`.
- **Ink rendering** → keep marks as a display list and repaint them each frame with
  `QPainter` (`Antialiasing` on). Only flatten to a pixmap on export.
- **HiDPI** → all values here are logical pixels. Grab at
  `devicePixelRatio` and export at native resolution; the dimension chip should report
  **logical** selection size, as the prototype does.

## Files

```
design_handoff_snipux_overlay/
├── README.md                          this document
├── tokens.py                          every colour, metric, font and behaviour constant
├── icons/*.svg                        22 glyphs, 24×24, currentColor
└── reference/
    ├── Snipux Overlay.dc.html         THE SPEC — open in Chrome, it is interactive
    ├── Snipux Editor.dc.html          rejected earlier directions, context only
    ├── Icon.dc.html                   icon component used by the two above
    └── support.js                     runtime for the two .dc.html files
```

Open `reference/Snipux Overlay.dc.html` directly from disk — no server needed.
