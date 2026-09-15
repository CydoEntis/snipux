# Handoff: snipux chooser + stills bar — LOCKED

**Status: design locked 2026-09-15.** Colours, sizes, radii, spacing and interactions are
final. Build to them.

## Overview

The two bars a still snip passes through, condensed from what's currently shipping and grown
to cover eleven new annotation features — at **no extra width**.

```
chooser (docked top)  →  drag a region  →  stills bar (centred under it)  →  destination
```

Both were already specified in `design_handoff_snipux_flow/`; this supersedes those two
sections. The recording bars, the stage machine, and the destination semantics (including why
`Copy` means something different for a video) are unchanged — keep using the flow handoff for
those.

What changed and why:

| Before | Now | Because |
|---|---|---|
| Chooser ≈700px, four labelled chips | **382px**, one label | `Last region` is a mode, not a flag; flags don't need labels |
| `Last region` as a chip | A row in the mode menu, showing the dimensions it restores | It answers *what to capture* |
| `Hide sensitive` as an accent chip | Icon toggle in a recessed well, eye-with-strike glyph | The accent was being spent on a background setting |
| Stills bar + a wider options tray, ~110px tall | **One 478px row**, options in a popover | The tray was the widest object on screen and restated the lit tool |
| 8 tools flat | 7 slots, two of them notched families | Room for ellipse, line, dashed, fills, blackout, watermark |

Target: **Python + Qt (PySide6 / PyQt6)**, Wayland primary, X11 must work.

## About the design files

`reference/*.dc.html` are **design references written in HTML** — running prototypes of the
intended look and behaviour, not production code. Rebuild them as Qt widgets.

| File | What it is |
|---|---|
| `reference/Snipux Handoff Preview.dc.html` | **THE SPEC.** Full-screen, both bars, one continuous snip. |
| `reference/Snipux Chooser Condensed.dc.html` | The chooser exploration — current build, 10a, 10b, plus the glyph candidates for Hide sensitive with the reasoning. |
| `reference/Snipux Tool Options.dc.html` | The stills-bar exploration — 11a, 11b, then 12a grown to all eleven features. Measures itself from the live DOM. |
| `reference/Icon.dc.html`, `reference/support.js` | Machinery the above need. Not deliverables. |

Open from disk in a Chromium-based browser. The spec is a complete snip: switch mode, cycle
the destination icon, toggle the flags, **drag anywhere** to frame a region (or pick `Window`
and click a pane), then draw with every tool, use both notch families, cycle fill and line
style, toggle the watermark, and fire the destination. `R W F L` set mode, `Space` reopens the
chooser, `Esc` restarts.

**Where this document is ambiguous, do what the spec does.** The two exploration files are
worth ten minutes first — each rejected direction carries a written note on why it lost, which
is the best defence against reintroducing one later.

---

## The two rules that keep both bars small

Everything below follows from these. They're restated at the top of `tokens_bars.py`.

### 1 · Control weight follows decision frequency

| Weight | For a decision re-made… | Which controls |
|---|---|---|
| **Labelled chip** | every snip | mode — the only one |
| **Icon button** | occasionally | destination, flags, tools |
| **Menu row** | once, then forgotten | Last region, watermark content |

A control's job is to **show its state**, not explain itself. Explanation lives in the tooltip
and the hint line. That's also what frees the accent `#e3ff4f` to mean exactly one thing:
*this is the thing you press*.

### 2 · Eight slots, no more

The stills bar holds eight tool slots. A new feature answers one of three questions before it
ships: which slot does it join as a **sibling** (notch family), which **popover section** does
it belong to, or is it really a **Settings preference**? Nothing gets a slot for being new.

Rounded-rect, polygon, callout, spotlight → shape siblings. Arrow-head style, corner radius,
text weight → popover. Default ink, watermark content → Settings.

---

## 1 · Chooser — 382 × 42

Flush to the top edge of the monitor the snip opened on. Square top corners, 12px bottom
corners, no top border — it hangs from the edge rather than floating near it. The whole
monitor is under the 62% scrim; **there is no selection yet, so no marching-ants rectangle
exists.**

Monitor, not desktop:

```python
screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
geo    = screen.geometry()
x      = geo.x() + (geo.width() - bar.width()) // 2
y      = geo.y()
```

Four bugs in the existing build came from using the virtual desktop. Every measurement in this
document is relative to `geo`.

Contents, left to right:

1. **Kind** — camera / ● in a recessed `#000` 34% well. Record is a filled 10px circle, not a
   glyph.
2. **Mode** — the only labelled control. Active mode's glyph in `#eaff7a`, then the name, then
   a chevron. Menu 240px: glyph, label, shortcut, tick. Below a rule: **Last region**, with
   the dimensions it restores as a mono subtitle and `⇧R`.
3. **Destination** — icon only, 28px, `#a8afa0`. Click cycles Copy → Save → Edit; the tooltip
   names the current one and its consequence. No label: the stills bar's split button restates
   it a moment later.
4. **Flags well** — `Hide sensitive` and `Delay` share one recessed well matching the kind
   pair's treatment. This is deliberate: inside a well, an unlit icon reads as **off**, where a
   bare unlit icon reads as *a button nobody has pressed*. Armed flags take `#eaff7a` on an 18%
   accent wash. Delay shows its value only when set.

Under the row, a hint pill: the active mode's accent glyph plus its next-step text.

**Picking a mode does not arm it.** The chooser stays open; arming happens when the user acts —
dragging, or clicking a window. `Full screen` is the exception: nothing left to aim at, so it
captures immediately. There is no primary button anywhere in the chooser; an earlier draft
ended the row with `Pick a window`, which promised an action it couldn't perform.

### Hide sensitive uses the eye-with-strike

Not the droplet. The droplet is the **blur tool's** glyph — it says "I will smudge this by
hand", where this feature says "the app finds and masks password fields, tokens and card
numbers". Candidates and their trade-offs are in the chooser reference file.

### Armed / marking-up tab

Once a region exists the chooser collapses to a 22px tab on the same edge at 70% opacity
(100% on hover), carrying mode, `then <destination>`, and — if armed — the eye-strike glyph.
**Flags carry through the stage change**; that's the point of keeping them visible. Click the
tab or press `Space` to reopen with everything intact.

---

## 2 · Stills bar — 478 × 42

Centred on the selection, 16px below it, clamped 12px from any monitor edge and to
`monitor_h − 108`. Centred, not right-anchored: a centred bar moves both edges when its width
changes, which is why no collapse mechanic survived review.

Contents, left to right: **split action → divider → 7 tool slots → divider → style dot,
watermark → divider → undo, clear.**

The action is at the **left end** and is the only accent-filled control. Picking a tool never
changes anything to its left.

### Tool order is a gradient of consequence

```
pen · highlighter · [shapes] · step · text · [redaction] · eraser
 ─────────────────────────────────────────────────────────────────►
 draw on top    frame    add content    destroy pixels    remove marks
```

**Never MRU-sorted, never reordered at runtime.** Muscle memory is the feature. Redaction sits
next to the eraser because both are destructive; it sits far from the pen because reaching for
one when you meant the other is the expensive mistake.

### Notch families

Two slots have siblings, and a notch always means the same thing: *this slot has more*. The
slot shows whichever sibling you used last; the corner triangle opens the family; each sibling
keeps its own shortcut so the menu is for discovery, not for use.

- **Shapes** — rectangle, ellipse, straight line, arrow. `R O L A`.
- **Redaction** — blur, pixelate, blackout. `B` cycles.

Redaction rows carry a security note, and it isn't decoration — blur on small text is famously
recoverable:

| Mode | Note |
|---|---|
| Blur | Softens it — shapes still readable |
| Pixelate | Blocky, obviously deliberate |
| Blackout | Solid bar. Nothing to reconstruct |

All three **bake destructively on export.** Blackout fills `#0b0c09`.

### Style dot + popover — 216px wide

One 28px button that **is** its own preview: a filled dot in the current colour at the current
stroke diameter. Outline-only shapes render it as a ring. You read colour and stroke without
opening anything.

The popover renders **only the sections the active tool supports** (`STYLE_SECTIONS`) —
nothing visible that can't do anything:

- **Colour** — seven swatches plus `+`, flush across one row.
- **Fill** (rect, ellipse) — one cycling button: outline → filled → both. Filled reuses the
  **stroke colour** at 22%/90%, so there's never a second colour picker to reconcile.
- **Line** (shapes) — one cycling button: solid → dashed → dotted.
- **Strength** (blur, pixelate) — slider.
- **Stroke / text size** — slider plus a mono readout.

Fill and line are **click-through**, matching the chooser's destination and delay: click
advances, the control shows the state. One behaviour to learn, and it's what got the popover
down from three rows to two.

**Style is per tool and remembered.** Setting a dashed red box leaves the pen a 5px acid line.
`DEFAULT_STYLE` in the tokens is the first-run seed.

**Tools with nothing to style** — blackout, eraser — dim the style dot to 34%, stop it
opening, and its tooltip reads *"Blackout has nothing to style"*. Same contextual rule, one
level up: a control that can't do anything shouldn't look live.

### 11a is conditional on the keyboard

The one-row layout is only better than a visible tray **if the shortcuts exist**: `1`–`7` for
colour, `[` / `]` for stroke, `D` to cycle line style, and the popover staying open until
dismissed rather than closing on each pick. Without them, ship the two-row variant (`11b` in
the reference) instead — one row without a keyboard is worse than what's shipping now.

### Watermark is not a tool

You don't draw it — it lands on the whole image, once, and you need to see where. So it's a
toggle with a notch for corner and opacity; **what the mark is** (logo, text, colour) is a
Settings preference. It previews live inside the selection at the chosen corner and opacity.

---

## Implementation notes

### The one that will bite

**Chrome must stop pointer propagation.** The overlay's canvas-wide press handler — the one
that closes menus and starts a selection drag — will fire before a click on a popover button
lands, tearing down the popover first. Every swatch, cycling button and menu row looks dead.
In Qt: don't let a canvas-wide `mousePressEvent` swallow presses meant for child widgets;
accept the event in the bar and its popups.

### Drag hygiene

A lost pointer-up leaves the in-progress mark live, and ordinary mouse movement then stretches
it until it covers the region — which looks like a rendering bug and isn't. Four guards:

- global `pointerup` / `pointercancel` / window focus-out all finalise the drag;
- no buttons held during a move event means the drag is over, whatever events were missed;
- a new press finalises any open drag first;
- the finaliser reads current state, not a captured snapshot.

A drag under 80 × 50 is discarded and returns to `choose` — not captured.

### The rest

- **`box-sizing: border-box`** or the Qt equivalent. The popover authored at 216px rendered
  238px without it.
- **`backdrop-filter`** has no Qt equivalent. The desktop behind is a static grab: blur each
  bar's region once, cache it, paint that crop behind the fill. Fallback: raise fill alpha to
  ~0.97 and skip the blur. Never a live blur.
- **Alpha is not opacity.** Bars are a 94%-alpha *fill* with fully opaque children.
  `windowOpacity = 0.94` washes out the icons. The legitimate uses of real opacity are the
  collapsed tab (0.70) and the disabled style dot (0.34).
- **Menus are top-level popups**, not children of a bar — an open menu must paint above the
  hint pill beneath it, and an effect-bearing parent traps it.
- **Square-top / rounded-bottom**: `QPainterPath` with per-corner radii, or `addRoundedRect`
  on a rect extended 12px above the visible top so the top corners clip off.
- **Marching ants**: dashed `QPen` + `setDashOffset` on a ~30fps timer. The frame draws
  **outside** the captured pixels.
- **Ink**: marks in screen coordinates, display list, `setClipRect(sel)`, repaint with
  `QPainter` (`Antialiasing` on). Flatten only on export.
- **Icons**: `icons/*.svg`, 27 glyphs, 24×24, `currentColor`, stroke 1.55, drawn for a 15px
  box in a 28px button. `eyeOff`, `blackout` and `mask` are new — don't substitute
  near-enough shapes; the whole point of the eye-strike is that it isn't the droplet.
- **Fonts**: IBM Plex Sans for chrome, IBM Plex Mono for every numeral, dimension, hex and
  shortcut. Ship both; sizes are fixed and the layout is tuned to them.

---

## State model

```
stage     : 'choose' | 'stills'
kind      : 'stills' | 'record'
mode      : str            # CAPTURE_MODES
dest      : 'Copy' | 'Save' | 'Edit'
delay     : str            # DELAYS
hide      : bool           # Hide sensitive
menu      : 'mode'|'shapes'|'redact'|'opts'|'wm'|None     # one at a time
sel       : QRect|None     # screen coords, min 80×50
hover     : rect|None      # Window mode preview
marks     : list[Mark]     # screen coords
tool      : str            # ANNOTATION_TOOLS
shape     : str            # last-used shape sibling
redact    : str            # last-used redaction sibling
strength  : int            # STRENGTH_RANGE
style     : dict[tool, {color, size, dash, fill}]          # per tool, remembered
wm, wm_corner, wm_alpha
screen    : QScreen        # the monitor everything positions against
```

Mode, destination, delay, flags and per-tool style persist across snips within a session;
initial values come from Settings. Nothing here writes back to `default.toml` — a per-snip
override must not silently change the user's preferences.

## Files

```
design_handoff_snipux_bars/
├── README.md                              this document
├── tokens_bars.py                         metrics, colours, tool order, style sections
├── icons/*.svg                            27 glyphs (eyeOff, blackout, mask are new)
└── reference/
    ├── Snipux Handoff Preview.dc.html     THE SPEC — both bars, one snip
    ├── Snipux Chooser Condensed.dc.html   chooser options + glyph candidates
    ├── Snipux Tool Options.dc.html        stills-bar options + the grown version
    ├── Icon.dc.html
    └── support.js
```

Recording bars, stage machine, destination semantics: `design_handoff_snipux_flow/`.
Overlay, Settings, review window: `design_handoff_snipux/`.

## Still open

1. **Freeform** still has no interaction design — the lasso, how the scrim inverts against a
   path, how export crops to the bounding box. It behaves as a region drag in the spec.
2. **Text tool** drops a label on click but has no in-place editing in the spec; the main
   handoff's mark model covers it.
3. **Hint lines** — whether they persist forever or retire after N successful snips. Suggest
   tying them to the same preference as the overlay's hint bar rather than a second toggle.
4. **Custom colour** (`+`) opens `QColorDialog`; whether a picked colour joins the seven
   swatches permanently or is session-only isn't decided.
