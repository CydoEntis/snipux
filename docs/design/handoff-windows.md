# Handoff: snipux

## Overview

A redesign of the snipux screenshot tool: the capture overlay, the Settings window, and
the review window that opens after a snip.

The current build opens a separate editor window with a single congested row of text
buttons, and the user cannot tell where the screenshot ends or what they are editing. The
Settings dialog is one shortcut row and one checkbox floating in an otherwise empty box.

This design removes the editor window entirely. The user drags a region, the screen
freezes, and they annotate **directly on the frozen desktop** inside the selection. The
selection stays live and resizable the whole time, so the boundary of the capture is
always visible. One floating bar sits under the selection; tool settings appear as a tray
only while a drawing tool is held.

The review window then shows what was captured and where it went — and can re-enter
annotation by revealing **that same floating bar** over the flattened image. One tool set,
one mark model, two places it can appear. Settings gets a nav rail so preferences have
somewhere to go, and leads with the shortcut recorder and a live GNOME conflict check.

Target implementation: **Python + Qt (PySide6 or PyQt6)**. The overlay is a frameless
full-screen window; Settings and review are ordinary windows.

## About the design files

The files in `reference/` are **design references written in HTML** — running prototypes
that show the intended look and behaviour. They are not production code and should not be
ported line by line. The task is to **rebuild these designs as Qt widgets** using the
project's existing patterns.

| File | What it is |
|---|---|
| `reference/Snipux Overlay.dc.html` | **The overlay.** Full-bleed, interactive. |
| `reference/Snipux Windows.dc.html` | **Settings (`1a`) and the review window (`1b`)**, side by side. Both interactive. |
| `reference/Snipux Editor.dc.html` | Rejected early exploration: two separate-editor-window layouts. Context only — do not build it. |
| `reference/Icon.dc.html`, `reference/support.js` | Machinery the files above need. Not a deliverable. |

Open them in a browser (Chromium-based; they use `backdrop-filter`) straight from disk, no
server needed. They are fully interactive — pick tools and draw, drag the selection edges,
press the keyboard shortcuts, record a shortcut in Settings, toggle the review window into
annotate mode. **Use them as the behavioural spec**: anything ambiguous in this README, do
what the prototype does.

In `Snipux Windows.dc.html` the two windows sit on a dark canvas with `1a` / `1b` badges,
and a segmented control above `1b` flips it between Review and Annotating. The badges, the
canvas, that control and the explanatory paragraphs are **scaffolding for the design
review** — not part of the product. Each window begins at its own title bar.

## Fidelity

**High-fidelity.** Colours, type sizes, radii, spacing and interaction behaviour are final
and should be matched. Exact values are in `tokens.py`; import from it rather than
re-typing literals. Where Qt cannot reproduce a CSS effect exactly (see *Qt notes*), match
the intent, not the mechanism.

---

## Screens / views

Three surfaces: the **overlay**, the **Settings window**, the **review window**. The
overlay is specified first and in most detail, because the other two borrow from it.

---

# 1 · Overlay

There is **one** view. It has no window chrome, no title bar, no menu bar.

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

## State management (overlay)

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

---

# 2 · Settings window

An ordinary window, 780 × 580, resizable (the nav rail is fixed at 182px; the content pane
takes the slack). Not a modal dialog — the user may want it open while they test a snip.

Reference: `Snipux Windows.dc.html`, panel `1a`.

Chrome is the **`Win` palette** in `tokens.py` — opaque neutral dark, not the overlay's
warm glass. Window body `#14161a`; title bar, nav rail and footer `#191c21`; 1px `#2a2e36`
outline; 12px radius. Metrics are in `WinMetric`.

Three regions: a 42px title bar, a body split into nav rail + content pane, and a 56px
footer. Only the content pane scrolls.

## Nav rail

182px, `#191c21`, 1px right rule `#23262d`, 12/10 padding, 2px between rows. Four rows from
`tokens.SETTINGS_NAV`: 16px icon + 12.5px/500 label, 9/10 padding, radius 8. Selected row
`#2c313c` with `#f3f5f9` text; idle `#a0a7b4`; hover `#20242b`. The version string
(`snipux 0.9.2 / Qt 6.7 · X11`) is pinned to the bottom in 11px `#5f6674`.

**Capture is first deliberately** — the shortcut is what people open Settings to fix.

Group headings inside the pane: 10.5px/600, uppercase, `.1em` tracking, `#616876`. Groups
are separated by 22px, or by a 1px `#22252c` rule where the change of subject is larger.

## Capture pane

### Shortcut recorder

A 38px-tall field showing the current combination in **mono 13px**, plus a `Record` button
beside it. This replaces typing `<Super><Shift>x` by hand.

- **Idle**: field `#191c21`, 1px `#2b2f36`, text `#e4e8ef`, reads e.g. `Control+Alt+S`.
- **Recording**: border becomes `#e3ff4f`, fill `#22262d`, text becomes
  `Press a combination…`, and a 7px `#e3ff4f` dot appears to its left, pulsing
  (opacity 1 → .35 → 1 over 1.1s). The button label becomes `Cancel`.
  The dot is **rendered only while recording** — do not fade a permanently-present dot.
- The next key-down with at least one modifier commits. Bare modifier presses are ignored
  (waiting for the real key). `Esc` cancels and keeps the old binding.
- Modifier order is normalised **Control, Alt, Shift, Super**, then the key name
  upper-cased — `Control+Alt+S`. Match this exactly; it is the string the conflict check
  and gsettings both use.

Qt: grab the keyboard for the duration (`grabKeyboard()` on the field) so the combination
does not fire whatever currently owns it while you are recording it.

### Shortcut conflict check

Directly under the field — not a tooltip, not a dialog on save. A 9/11 padded box, radius 8:

- **Clear**: `#a0c85a` at 10% fill, at 24% border, `#a8c86a` text and a tick glyph.
  *"No GNOME shortcut uses Control+Alt+S."*
- **Clash**: `#c85050` at 12% fill, at 28% border, `#e8a5a5` text and a cross glyph.
  *"Control+Alt+T is already GNOME's “Launch terminal”. GNOME will not warn you — it will
  just fire the wrong one."*

Then a permanent 11.5px `#6d7484` explanation of why the check exists.

`tokens.GNOME_KNOWN` holds a **sample** table so the prototype can demonstrate both states.
The real check must read the live schemas — `org.gnome.desktop.wm.keybindings`,
`org.gnome.settings-daemon.plugins.media-keys`, and each entry in
`org.gnome.settings-daemon.plugins.media-keys.custom-keybindings` — and name the owner it
found. Re-run it on every recorded combination, and again on Save in case the desktop
changed underneath. A clash is a **warning, not a block**: the user may genuinely want to
take the key over.

### After capture

Three radio cards from `tokens.AFTER_CAPTURE`, not a checkbox — "open a review window" is
one of three real behaviours, and the old checkbox hid the other two. Each card: 15px ring
(`#e3ff4f` when selected, `#4a505b` idle) with a 7px dot, then a 12.5px/500 label and an
11.5px `#79808f` note. Selected card `#1e2229` fill with a `#3a3f49` border.

Below them, a switch: **Always copy to clipboard too** — orthogonal to the three, which is
why it is a switch and not a fourth card.

Switch anatomy (used throughout Settings): 34 × 19 track, radius 99, `#e3ff4f` on /
`#33383f` off, 15px `#f1f3e8` knob, 2px inset.

## Saving pane

- **Folder** — read-only mono field + `Choose…` (`QFileDialog.getExistingDirectory`).
- **Filename** — a text field holding a strftime-ish pattern, default
  `Screenshot from %Y-%m-%d %H-%M-%S`. Under it, a **live preview** of the resulting full
  path in mono `#c8d96a`, updating as you type, with the extension following the chosen
  format. Under that, `tokens.FILENAME_TOKENS` as clickable chips that append to the
  pattern — tokens are discoverable by clicking, not by remembering.
- **Format** — segmented `PNG / JPEG / WebP`. A **Quality** slider (40–100, default 88)
  appears only for the lossy two.
- **Save at native resolution** — switch. On writes 2× pixels on HiDPI; off saves what the
  user saw. Its note says exactly that.

## Annotation pane

Sets the overlay's *opening* state, so the controls mirror the overlay's own tray:

- **Tool the overlay opens with** — a segmented row of six 34px icon buttons
  (`tokens.INK_TOOLS`-equivalent: pen, highlighter, arrow, rect, step, blur).
- **Default ink** — the shared `INK_SWATCHES`, at **30px** here rather than the tray's 22px
  (a settings pane can afford the bigger target), same double-ring selected treatment.
- **Stroke** — slider 1–26 plus a 44px preview well showing a filled dot at the live
  diameter in the live colour.
- **Redaction** — segmented `Blur / Pixelate`, with the same "baked into the exported
  pixels" note the overlay tray carries.
- **Remember my last tool instead** — switch. When on, the tool row above is the
  first-run seed only.

## Tray & startup pane

Four switches from `tokens.TRAY_TOGGLES`, each with a note where the consequence is not
obvious. Nothing else — the pane is allowed to be short.

## Footer

56px, `#191c21`, 1px top rule. Left: a dirty indicator — `Everything saved` in `#6d7484`,
or **`Unsaved changes`** in `#c8a54a` once anything changes. Right: `Cancel` (secondary)
and **`Save`** (`#e3ff4f` fill, `#15170e` text, tick glyph).

Nothing applies live; Save commits the lot and rebinds the shortcut immediately. Cancel with
a dirty state should confirm before discarding.

---

# 3 · Review window

1020 × 700, resizable, opens after a capture when *After capture* is `Open a review
window`. Reference: `Snipux Windows.dc.html`, panel `1b`.

It answers the two questions the old window could not: **what did I capture**, and **where
did it go**. Then it lets the user keep annotating without launching a different editor.

Same `Win` chrome as Settings. Three regions: 42px title bar, the canvas, a footer.

## Title bar

Accent-square app mark, then the **filename** (`Screenshot from 2026-08-25 10-38-28.png`)
in 12.5px/500, then the pixel size in mono 11px `#6d7484`. Minimise / maximise / close at
the right; close hovers `#c0392b`.

## Canvas

The whole point of the redesign. Radial workspace (`Gradient.WORKSPACE`), and the
screenshot centred on it with a **1px `#454b56` border, a large soft drop shadow, and a
7px `rgba(255,255,255,.02)` outer ring**. The image has an edge. You can see where it
stops.

- **Top-left badge**: mono `1377 × 936` · `2 marks` (singular `1 mark`).
- **Top-right**: zoom cluster — minus / percentage / plus, 60–160% in steps of 20.
- Both badges are `rgba(18,20,24,.88)` on a 1px `#262a31`, radius 7-8, with a 6px backdrop
  blur, and sit **above** the image (z-order) so they are never occluded.

## Footer

13/16 padding. Left, stacked: a **status line** and the **path**.

- Status: tick glyph + `Saved` in `#9ec46a` — or pen glyph + **`Edited — not saved`** in
  `#c8a54a` the moment any mark changes.
- Path: the full destination in mono 11.5px `#8a92a1`, ellipsised, and **clickable** —
  it reveals the file, same as `Show in Folder`. Users go looking for the file, so the
  answer is in the window and it is actionable.

Right, in order: `Annotate` (toggles, label becomes `Done annotating`), `Show in Folder`,
`Save As…`, and **`Copy`** as the accent primary. `Copy` and `Save As…` both clear the
dirty state.

## Annotate mode

Pressing `Annotate` **reveals the overlay's own floating bar** over the image, bottom
centre, 18px from the canvas floor — the same widget, the same 34px buttons, the same
conditional settings tray, the same eight tools, the same mark model. It is not a second
editor and must not become one: build the bar and the mark layer as reusable widgets the
overlay and this window both instantiate.

Differences from the overlay, and they are the only ones:

- No capture-mode chip (there is nothing left to capture).
- The trailing action is **`Done`** (accent, tick) instead of Copy/Save — the footer already
  owns the exports.
- Marks are stored in **image coordinates**, not screen coordinates, because the image is
  the document here. Scale pointer positions by `image_width / displayed_width` so drawing
  stays correct at any zoom.
- Drawing anything flips the footer status to `Edited — not saved`.

Everything else — tool behaviour, the tray's swatches and stroke slider, blur/pixelate,
eraser-only hit-testing, undo/redo semantics — is identical to the overlay spec above.

Marks stay **live and editable** for the window's lifetime; they are flattened only on
Copy, Save As, or Save. Redactions bake destructively at that point, as specified.

## State management (windows)

```
# Settings
nav            : str          # 'capture' | 'saving' | 'ink' | 'tray'
combo          : str          # normalised, e.g. 'Control+Alt+S'
recording      : bool
conflict       : (owner: str | None)
after          : str          # 'review' | 'clip' | 'file'
clip, hidpi, remember : bool
pattern        : str
fmt            : str          # 'PNG' | 'JPEG' | 'WebP'
quality        : int          # 40–100, lossy only
ink_tool       : str
ink            : str          # hex
stroke         : int
blur_mode      : 'blur'|'pix'
tray_on        : dict[str, bool]
dirty          : bool

# Review
editing        : bool         # annotate mode
saved          : bool         # false once marks change
zoom           : int          # 60–160
marks, redo    : list[Mark]   # IMAGE coords; same Mark record as the overlay
tool, color, size, strength, blur_mode   # shared with the overlay's tray
```

Settings persists to `~/.config/snipux/profiles/default.toml` on Save. The overlay reads it
fresh per capture, so toggling a preference takes effect on the next snip without a
restart.

## Design tokens

All of them are in **`tokens.py`** — two palettes (`Color` for the overlay's glass, `Win`
for the windows' opaque chrome), the shared ink swatch list, the type scale, every metric
and radius (`Metric` for the overlay, `WinMetric` for the windows), shadow parameters,
shortcut map, capture modes, Settings nav and option lists, filename tokens, tool hints.
Import it; do not re-type values into widget code.

Three things worth restating because they are easy to get wrong:

- **Type**: IBM Plex Sans for all chrome, IBM Plex Mono for **every** numeral, dimension,
  path, shortcut combination and hex readout. Ship both with the app
  (`QFontDatabase.addApplicationFont`) rather than relying on system fonts, and set fixed
  pixel sizes — the layout is tuned to them.
- **Alpha is not opacity**: the overlay bar is a 93%-alpha *fill*, not a 93%-opacity
  widget. Its children are fully opaque. Painting the widget at 0.93 opacity will wash out
  the icons. (The windows have no alpha at all — do not give them any.)
- **Two palettes, one accent**: never use the overlay's warm `#1a1c18` in a window, or the
  windows' cool `#191c21` in the overlay. `#e3ff4f` is shared and means "press this" or
  "this is your ink" — nothing else gets it.

## Assets

- `icons/*.svg` — every glyph the three surfaces use, 24×24, `stroke="currentColor"`,
  stroke-width 1.55, round caps and joins. Load with `QIcon`, or recolour by parsing and
  substituting `currentColor` at load time. Drawn to sit in an 18px box inside a 34px
  button; the Settings nav uses them at 16px and the status lines at 13–15px.
- Fonts: IBM Plex Sans and IBM Plex Mono, SIL Open Font License, from the IBM Plex
  release. Not included here — pull weights 400/500/600 for Sans and 400/500 for Mono.
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
- **The windows** need none of the above except shadows and hover: they are ordinary
  opaque `QWidget`s with real layouts. Use `QVBoxLayout`/`QHBoxLayout` with the gaps from
  `WinMetric` rather than absolute positioning, so they resize. The switches and radio
  cards are custom-painted (Qt's native `QCheckBox`/`QRadioButton` will not match) — build
  them once as `Switch` and `RadioCard` widgets and reuse.
- **Share the bar** → the floating bar, the settings tray and the mark layer are used by
  both the overlay and the review window. Factor them as three widgets taking a mark model
  and a coordinate transform, not as two copies. The prototype does this literally: `1b`'s
  bar markup is the overlay's, minus the capture chip.

## Build order

1. The overlay, Region mode, with the shared bar / tray / mark-layer widgets.
2. The review window — it reuses all three, so it is mostly chrome and the footer.
3. Settings, starting with the shortcut recorder and conflict check.
4. The remaining capture modes (Window, Full screen, Freeform) and Delay.

## Files

```
design_handoff_snipux/
├── README.md                          this document
├── tokens.py                          every colour, metric, font and behaviour constant
├── icons/*.svg                        34 glyphs, 24×24, currentColor
└── reference/
    ├── Snipux Overlay.dc.html         THE OVERLAY — interactive
    ├── Snipux Windows.dc.html         SETTINGS (1a) + REVIEW WINDOW (1b) — interactive
    ├── Snipux Editor.dc.html          rejected earlier directions, context only
    ├── Icon.dc.html                   icon component the three above use
    └── support.js                     runtime for the .dc.html files
```

Open the `reference/*.dc.html` files directly from disk in a Chromium-based browser — no
server needed.

## Open questions for the team

Three calls I flagged rather than made:

1. **`Esc` in the overlay** discards ink but keeps the overlay open. Should it also close?
   A two-stage Esc (ink first, then dismiss) is my suggestion.
2. **`Copy` in the review window** leaves the window open so you can keep marking. Windows
   Snip closes on copy. Pick one.
3. **Capture modes beyond Region** (Window, Full screen, Freeform, Delay) are specified but
   not prototyped — worth a design pass once Region is real.
