# Handoff: snipux capture flow — LOCKED

**Status: design locked 2026-08-27.** Colours, sizes, radii, spacing, stages and
interactions in this document are final. Build to them.

## Overview

One continuous surface covering a whole snip, stills or video:

```
chooser  →  aim  →  ┬─ stills:  mark up → destination
                    └─ record:  ready → countdown → recording → stopped → destination
```

Three bars, one visual object changing state: the **chooser** (docked to the monitor's top
edge), the **stills bar**, and the **recording bar** (both floating under the selection).
They never coexist.

Two decisions, asked in the order they matter: **what to capture** (Region / Window / Full
screen / Freeform) and **what happens to it** (Copy / Save / Open). Delay lives with them.

Target: **Python + Qt (PySide6 / PyQt6)**, Wayland primary, X11 must work.

Related handoffs, unchanged by this one: `design_handoff_snipux/` (overlay, Settings, review
window) and `design_handoff_snipux_chooser/` (the chooser's monitor-anchoring and conflict
rules, which still apply).

---

## THE THREE RULES

These were each arrived at by building the alternative and rejecting it. Breaking one
undoes the design.

### 1. Every bar is centred on the region

The chooser is centred on the **monitor**; every post-selection bar is centred on the
**selection**, 16px below its bottom edge, clamped 12px from any monitor edge and to
`monitor_h − 108` vertically.

Right-anchoring was tried (so a growing drawer wouldn't move the action button) and dropped
along with the drawer. Bars must not shift sideways between stages.

### 2. No collapsing — tools are always visible

A collapsed face (generic pen, or the armed tool) and a slide-out drawer were both designed
and rejected. With a centred bar, any width change moves **both** edges, so no collapse
mechanic reads well. There is also no `Region ˅` chip on the post-selection bars: mode
cannot change once a selection exists, and a control that opens a menu it can't act on is a
lie. The dimension chip above the selection already says what you've got.

### 3. The primary action sits at the LEFT end

Order in every bar that has one: **action group → divider → everything else.** The action is
the only accent-filled control, and picking a tool never changes anything to its left.

---

## About the design files

`reference/*.dc.html` are **design references written in HTML** — running prototypes of the
intended look and behaviour, not production code. Rebuild them as Qt widgets using the
project's existing patterns.

| File | What it is |
|---|---|
| `reference/Snipux Flow.dc.html` | **THE SPEC.** Full-screen, fully interactive, the whole flow. |
| `reference/Snipux Capture Bar.dc.html` | The exploration: every rejected direction with a written note on why it lost. Context only — do not build. |
| `reference/Icon.dc.html`, `reference/support.js` | Machinery the two above need. Not deliverables. |

Open from disk in a Chromium-based browser (they use `backdrop-filter`). The spec is
genuinely interactive: pick a mode, drag a region on the desktop, draw with the tools, run a
recording with the live clock, open every dropdown. **Where this README is ambiguous, do what
the prototype does.**

`Snipux Capture Bar.dc.html` is worth ten minutes before you start — it is the best defence
against re-introducing a rejected idea later.

---

## Stage by stage

State machine: `choose → armed → (stills | recArmed → count → live → done)`.
`tokens_flow.STAGES` carries each stage's label and hint text.

### 1 · Choose

The chooser row, flush to the top edge of the monitor the snip opened on, square top corners
and 12px bottom corners, no top border. 42px tall. The whole monitor is under the 62% scrim
— there is no selection yet, so **no marching-ants rectangle exists**.

Contents, left to right:

| Control | Behaviour |
|---|---|
| **Kind** — camera / ● | Segmented pair in a `#000` 34% well, 28px buttons. Record is a filled 10px circle, not a glyph. Switching kind changes only what the bar becomes after the selection. |
| **Mode** — icon + label + chevron | The only labelled control. Active mode's glyph in `#eaff7a`. Menu is 228px: glyph, label, shortcut letter, tick. |
| **Destination** — icon only | 28px, `#a8afa0`. Menu is 284px with a two-line row per destination (label + consequence). Secondary decision, so no label on the trigger. |
| **Delay** — timer icon | Label appears **only when set**, in `#eaff7a`. Menu 146px. A forgotten countdown is the one thing here that can surprise you. |

Under the row, a hint pill carrying the active mode's accent glyph and its next-step text.

**Picking a mode does not arm it.** It updates the mode and the chooser stays open — this was
a bug report. Arming happens when the user acts: dragging a region, or clicking a window.
`Full screen` is the exception: it has nothing to aim at, so choosing it captures the whole
monitor immediately (after any delay).

No primary button anywhere in the chooser. An earlier draft ended the row with
`Pick a window` / `Drag to select`, which promised an action it could not perform.

### 2 · Aim (armed)

The chooser collapses to a 22px tab still attached to the same edge, at 70% opacity rising
to 100% on hover, carrying mode + destination. Click it or press `Space` to reopen the full
row with selections intact. The next-step hint reappears centred below it with a 180ms rise.

- **Region / Freeform** — drag. A drag under 60 × 40 is discarded and returns to `choose`,
  not captured.
- **Window** — the window under the cursor is outlined live in `#e3ff4f` (85% border, 7%
  fill) with a name + size chip; click takes it. In the prototype the two mock "windows" are
  the sidebar and the content pane; in the real app enumerate the compositor's windows.

Selection frame: white 2px drawn at `-3px` — **outside** the captured pixels — plus animated
marching ants (`7 7` dash, offset −14 over 700ms), 24px corner brackets, and 30 × 8 edge
handles. Above it: the light dimension chip (`963 × 596 · 0 marks`, singular `1 mark`) at the
left, the `Frozen` pill at the right, both 34px up.

### 3a · Stills — mark it up

Bar contents: **split action button → divider → 8 tools → divider → undo, clear ink.**

- Tools from `ANNOTATION_TOOLS`, 28px each; active one `#ffffff` 16% with `#f8faf0` glyph.
- Undo greys to `#5d6157` on an empty stack. Clear-ink hovers red.
- Hint line under the bar always states the **active tool's** instruction.
- Marks are stored in **screen coordinates** and the ink layer is clipped to the selection,
  so a mark that falls outside is hidden, not deleted (see the main handoff for the mark
  model, per-tool geometry, and the destructive-blur requirement).

### 3b · Recording — ready

Bar contents: **Record → divider → audio → delay → divider → Cancel.**

- **Record** is the accent action: filled 10px circle + label + `↵`.
- **Audio** is a dropdown, not a cycling button — System / Mic / Muted from `AUDIO_SOURCES`,
  each with a note on what it actually records, tick on the current one, opening **upward**
  so it never covers the region.
- **Delay** is its own dropdown in this bar too (it had no render site here at first — a
  visibly-enabled control doing nothing, in the one stage where a countdown matters).
- Resize handles are **live only in this stage**, and the hint says so: *"Reframe now — you
  cannot resize once it is rolling."*

### 4 · Countdown

The numeral goes **inside the region**, centred — where the user is already looking — in a
118px circle at 54px mono. The bar reduces to an armed pill plus Cancel. `Esc` aborts.

### 5 · Recording (live)

- Scrim drops from 62% to **28%** so the user can see what they're filming.
- Selection frame switches to solid `#ff5a52`, still 3px outside the captured pixels.
- Dimension chip goes dark with a blinking red dot and appends `30 fps`.
- Bar: **clock (mono, red wash) → Stop → Pause → divider → audio + size readout.** Red
  appears nowhere else in the product, which is why it can mean "live" without a label.
- The bar sits **below the region and outside the frame** — it is not in the file. The hint
  says so, because this was a real bug report.

### 6 · Stopped

Bar: **split action → divider → summary chip (`00:27 · 11.3 MB · webm`) → discard.** Then the
same destination logic as stills.

---

## Destinations

`tokens_flow.DESTINATIONS`. The chooser sets the split button's face; the chevron always
offers the other two; `C` / `S` / `O` fire them directly so a habit never costs a menu.

**Copy is not the same operation for a recording.** A still goes on the clipboard as image
data and pastes anywhere. A video can only go on as a **file reference** — it pastes into a
file manager, Slack, Discord or an upload field, and does nothing in an image editor or a
text box. So for recordings the label reads `Copy file` and the toast reads *"File copied —
paste into a chat or folder"* instead of *"Copied to clipboard"*. Same key, honest wording.

**All three write the file first**, Copy and Open included. A capture that exists only on a
clipboard is one Ctrl+C from gone, and every bug report starts with "where did it go".

Toast: bottom centre, 70px up, `#e9ecf2` at 96%, radius 11, 12.5px/500, 180ms rise, ~1.7s.

---

## Keyboard

`tokens_flow.SHORTCUTS`.

| Key | Action |
|-----|--------|
| `R` `W` `F` `L` | set mode (does **not** arm, except `F`) |
| `Space` | reopen the chooser from the armed tab |
| `Enter` | fire the stage's primary action — arm, Record, or the destination |
| `Esc` | close an open menu; else stop a recording; else cancel the snip |
| `C` / `S` / `O` | Copy / Save / Open directly |
| `Ctrl+Z`, `Ctrl+Shift+Z` | undo, redo |

Suppress all of these while a text label has focus.

---

## State model

```
phase    : 'choose'|'armed'|'stills'|'recArmed'|'count'|'live'|'done'
kind     : 'stills'|'record'
mode     : str          # CAPTURE_MODES
dest     : 'Copy'|'Save'|'Open'
delay    : str          # DELAYS
menu     : 'mode'|'dest'|'delay'|'audio'|None      # one at a time
sel      : QRect|None   # screen coords, min 60×40
hover    : window rect|None                        # Window mode preview
marks    : list[Mark]   # screen coords; see main handoff
tool     : str          # ANNOTATION_TOOLS
count    : int          # countdown remaining
elapsed  : int          # seconds recorded
paused   : bool
audio    : 'system'|'mic'|'off'
toast    : (text, glyph)|None
screen   : QScreen      # the monitor everything positions against
```

Mode, destination, delay and audio persist across snips within a session; initial values come
from Settings. Nothing here writes back to `default.toml` — a per-snip override must not
silently change the user's preferences.

---

## Qt notes

- **`backdrop-filter: blur(16px)`** has no Qt equivalent. The desktop behind is a static
  grab: blur each bar's region once, cache it, paint that crop as the widget background
  before the fill. Fallback: raise fill alpha to ~0.97, skip the blur. Never a live blur.
- **Alpha is not opacity.** Bars are a 93%-alpha *fill* with fully opaque children;
  `windowOpacity = 0.93` washes out the icons. The two places opacity is genuinely correct
  are the armed tab (0.70 → 1.0 on hover) and nothing else.
- **Menus must be top-level popups**, not children of a bar. An open menu has to paint above
  the hint pill below the bar, and an effect-bearing parent traps it. (The HTML reference hit
  exactly this with `backdrop-filter` creating a stacking context.)
- **Square-top / rounded-bottom** on the chooser: `QPainterPath` with per-corner radii, or
  `addRoundedRect` on a rect extended 12px above the visible top so the top corners clip off.
- **Marching ants**: dashed `QPen` + `setDashOffset` on a ~30fps timer.
- **Ink**: keep marks as a display list, repaint with `QPainter` (`Antialiasing` on),
  `setClipRect(sel)`. Flatten only on export; bake redactions destructively.
- **Monitor, not desktop**: `QGuiApplication.screenAt(QCursor.pos()).geometry()` is the frame
  everything positions against. Four bugs in the existing build came from using the virtual
  desktop. On Wayland prefer a layer-shell surface anchored to the target output.
- **Structure**: one frameless always-on-top widget per bar with real child widgets for the
  controls (tooltips, hover, focus come free), plus separate popups for menus, the hint pill,
  the tab and the toast. The scrim is painted by the overlay window underneath.
- **Icons**: `icons/*.svg`, 35 glyphs, 24×24, `currentColor`, stroke 1.55, drawn to sit in a
  16px box inside a 28px button. Recording needs `speaker · mute · mic · play · pause ·
  record · stop`; substituting near-enough shapes is how a droplet ends up meaning "audio
  source".
- **Fonts**: IBM Plex Sans for chrome, IBM Plex Mono for every numeral, dimension, clock,
  size and shortcut. Ship both with the app; sizes are fixed and the layout is tuned to them.

---

## Files

```
design_handoff_snipux_flow/
├── README.md              this document
├── tokens_flow.py         metrics, colours, stages, destinations, shortcuts
├── icons/*.svg            35 glyphs, 24×24, currentColor
└── reference/
    ├── Snipux Flow.dc.html          THE SPEC — interactive, full flow
    ├── Snipux Capture Bar.dc.html   rejected directions + reasoning
    ├── Icon.dc.html
    └── support.js
```

## Still open

Not blockers for this build, but unresolved:

1. **Freeform** has no interaction design — the lasso, how the scrim inverts against a path,
   how export crops to the bounding box. It behaves as a region drag in the prototype.
2. **Pause/resume semantics** — one continuous file, or segments concatenated on stop.
3. **The review window's video mode** (player, trim, mute, GIF export) is described but not
   designed. Its still-image mode is in the main handoff.
4. **Hint lines** — whether they persist forever or retire after N successful snips. Suggest
   tying them to the same preference as the overlay's hint bar rather than a second toggle.
