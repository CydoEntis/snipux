# Handoff: snipux pre-snip chooser

## Overview

The chooser is the first thing a snip shows: a single 54px row hanging from the top edge of
the active monitor, asking the two questions that have to be answered before anything is
captured.

1. **What to capture** — Region, Window, Full screen, Freeform
2. **What happens to it** — open the review window, copy and get out of the way, save silently

Plus **delay** (Off / 3s / 5s / 10s), which previously lived in the mode popover on the
floating bar. That popover is gone; this replaces it.

It exists because capture mode used to live on the floating bar, and that bar only appears
once a selection exists — so choosing "window" meant dragging out a region you didn't want,
clicking a chip, picking the mode you actually wanted, and watching the region get thrown
away. Mode is the first decision of a snip, so it is now asked first.

This document covers **only the chooser**. The overlay, Settings window and review window
are specified in the main handoff (`design_handoff_snipux/`), and none of them change.

Target implementation: **Python + Qt (PySide6 / PyQt6)**, Wayland primary, X11 must work.

## About the design files

`reference/*.dc.html` are **design references written in HTML** — running prototypes showing
intended look and behaviour. They are not production code and should not be ported line by
line. Rebuild them as Qt widgets using the project's existing patterns.

| File | What it is |
|---|---|
| `reference/Snipux Chooser.dc.html` | **THE SPEC.** The chosen design, full-bleed and interactive. |
| `reference/Snipux Capture Bar.dc.html` | The exploration that led here — five rejected directions with their reasoning. Context only. |
| `reference/Icon.dc.html`, `reference/support.js` | Machinery the two above need. Not deliverables. |

Open them from disk in a Chromium-based browser (they use `backdrop-filter`). The spec file
is fully interactive: open all three dropdowns, press `R` `W` `F` `L` to switch mode, press
`Space` to reopen the chooser, `Esc` to cancel. **Where this README is ambiguous, do what
the prototype does.**

`Snipux Capture Bar.dc.html` is worth ten minutes before you start. Its `4a` is what
shipped; `3a`, `3b`, `3c`, `2a`, `2b`, `2c` are the alternatives and each carries a written
note on why it lost. That reasoning is the best defence against re-introducing a rejected
idea later.

## Fidelity

**High.** Colours, sizes, radii and spacing are final. Values live in `tokens_chooser.py`
plus the main handoff's `tokens.py` — import both rather than re-typing literals. Where Qt
cannot reproduce a CSS effect exactly, match the intent (see *Qt notes*).

---

## The surface

### Position — against a monitor, never the desktop

The chooser is **horizontally centred on, and flush with the top edge of, the monitor the
snip opened on.** Not the virtual desktop.

The target machine is three staggered monitors on a 6400 × 1440 virtual desktop with dead
zones between them. Centring on the desktop puts the chooser in a gap. Four separate bugs
in the existing build came from conflating the two, so this is worth being explicit about:

```python
screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
geo    = screen.geometry()          # the ACTIVE monitor, in virtual-desktop coords
x      = geo.x() + (geo.width() - panel.width()) // 2
y      = geo.y()                    # flush to that monitor's top
```

Every other measurement in this document — the keyboard legend at the bottom, the armed
hint, the scrim — is likewise relative to `geo`, never to the virtual desktop.

The panel has **square top corners and 14px bottom corners, and no top border**. It reads as
hanging from the edge rather than floating near it. That is deliberate: it is the visual
claim that this bar belongs to this monitor.

### Backdrop

The chooser sits on the frozen desktop grab under the overlay's standard scrim —
`#0c0d0a` at 62%, flat, no blur. **The entire monitor is scrimmed**: there is no selection
yet, so there is no undimmed region and no marching-ants rectangle. (Reviewers keep drawing
one. There isn't one.)

Chrome is the overlay's warm glass: `#1a1c18` at 93%, 1px `#ffffff` at 10%, `backdrop-filter:
blur(16px)`, drop shadow 48px blur / 22px down / black at 62%.

---

## Anatomy

A single row, 54px tall (10 pad + 34 control + 10 pad), 8px gaps, intrinsically ~420px wide.
Left to right it reads as a sentence: **`Window` then `Review` `No delay`**.

### 1. Mode dropdown

Trigger: 34px tall, radius 9, transparent fill, 1px `#ffffff` at 10%. Contents are the
active mode's 16px icon **in `#eaff7a`**, the mode name in 12.5px/500 `#f1f3e8`, and a 14px
chevron in `#8f9689`. The accent icon is what makes the armed state legible at a glance.

Menu: 250px, opens 7px below the trigger. Rows are 16px icon + 12.5px/500 label + the
shortcut letter in mono 10.5px `#6f766a` + a `#e3ff4f` tick on the selected row. Row padding
8/9, radius 8. Selected row `#ffffff` at 8% with `#f8faf0` text; hover `#ffffff` at 9%.

Options and their order come from `tokens.CAPTURE_MODES`.

### 2. "then"

A literal 12px/400 `#8f9689` text node between the mode and destination triggers. It is not
decoration — it is what makes the row parse as one sentence instead of three widgets.

### 3. Destination dropdown

Same trigger anatomy; icon is 15px in `#a8afa0` (not accent — this is the secondary
decision). Menu is 270px, the widest of the three, because its rows are two-line: a
12.5px/500 label over an 11px `#8f9689` note explaining the consequence. Options from
`tokens.AFTER_CAPTURE`.

### 4. Delay dropdown

Same trigger, with the timer glyph. Label is the value verbatim — `No delay`, `3s`, `5s`,
`10s`. When a delay **is** set, the label turns `#eaff7a`, so a countdown that is about to
surprise you is visible before it does. Menu is 152px, label-only rows.

### 5. Next-step hint

8px below the panel, centred on it: a 24px pill, `#101210` at 72%, 1px `#ffffff` at 7%,
radius 8, holding the active mode's accent icon and a line of 11.5px `#8f9689` text from
`MODE_NEXT_STEP`.

**There is no primary button, and this line is why.** An earlier draft ended the row with a
filled `Pick a window` / `Drag to select` button, which was a lie: only Full screen has
anything for a button to do. The other three modes need the chooser to get out of the way so
the user can drag, hover or trace. So picking the mode **is** the commit, and what happens
next is *stated* rather than *promised*.

### 6. Keyboard legend

Bottom centre of the active monitor, 26px up: `R W F L` mode · `Space` reopen · `Esc` cancel.
Key names mono in `#d7dacb`, everything else 11.5px `#8f9689`. Same glass, radius 10.

Reasonable to hide this after N successful snips — same treatment as the overlay's top hint
bar.

---

## States

There are two, and the second one is the whole design problem.

### Choosing

Everything above is visible. No mode is armed; the pointer is a normal arrow. Nothing has
been captured and nothing is being previewed.

### Armed

Reached by picking a mode — from the dropdown, or by pressing its shortcut.

The panel **collapses to a 26px tab**, still attached to the same top edge, still centred.
The tab carries the mode icon and name in `#eaff7a`, `then <destination>` in `#8f9689`, the
delay in mono if one is set, and a chevron. It renders at **72% opacity, rising to 100% on
hover** (160ms ease). Clicking it — or pressing `Space` — reopens the full panel with all
selections intact.

The next-step hint reappears centred 52px from the top, with a 180ms rise-and-fade, then
stays. The cursor becomes a crosshair. Window mode begins previewing whatever is under the
pointer with a `#e3ff4f` frame at 85% and a 7% fill, and a mono size chip above it.

**Why a tab and not nothing:** the dead band is 26px of the monitor's top edge, which on
GNOME is already the top bar's territory — so in practice the chooser costs nothing that
wasn't already spoken for. Window previews and Freeform tracing work everywhere below it.
Alternatives were considered and rejected: collapsing to a pill at *bottom* centre (3a) put
a translucent object over live desktop; vanishing entirely onto the cursor (3c) left the
destination unreadable and un-changeable without a keystroke.

### Full screen is the exception

`Full screen` has nothing left to aim at, so selecting it does not arm — it **fires the grab
immediately**, after any configured delay. `IMMEDIATE_MODES` in the tokens carries this.
Its hint line says so: *"Grabs this monitor the moment you choose it."*

---

## Behaviour

### Keyboard

| Key | Action |
|-----|--------|
| `R` `W` `F` `L` | select Region / Window / Full screen / Freeform, and arm it |
| `Space` | reopen the chooser from the armed tab |
| `Esc` | close any open dropdown; if none is open, cancel the snip entirely |
| `Return` | nothing — deliberately. There is no default action to confirm. |

Shortcuts are live in both states. Suppress them while a dropdown has keyboard focus and is
being navigated with arrows.

### Dropdowns

One at a time — opening one closes the others. The open trigger takes a `#ffffff` 9% fill and
a 20% border so it is visibly the source of the menu. Click-outside closes. Menus are
`Qt.Popup` children so they can paint outside the panel's bounds.

Menus open **downward**, over the scrimmed desktop. That is acceptable here in a way it was
not for the floating bar's popover: nothing is selected yet, so there is nothing underneath
worth not covering.

### Handoff to the floating bar

The chooser **seeds the floating bar's mode chip**. When the user completes a selection, the
chooser is gone and the bar appears with the same mode already set; changing mode on the bar
later and re-opening the chooser shows the changed value. One piece of state, two surfaces —
do not duplicate it.

The chooser and the floating bar **never coexist**: chooser up means no selection, bar up
means a selection exists. They may safely share a widget stack.

### Persistence

Mode, destination and delay persist across snips within a session. On launch they come from
Settings (`after_capture`, and the annotation pane's defaults). Nothing here writes back to
`default.toml` — a per-snip override should not silently change the user's preferences.

---

## State model

```
mode        : str    # 'Region' | 'Window' | 'Full screen' | 'Freeform'
after       : str    # 'Review' | 'Copy' | 'Save'
delay       : str    # 'No delay' | '3s' | '5s' | '10s'
phase       : str    # 'choosing' | 'armed'
menu        : str | None    # 'mode' | 'after' | 'delay' | None
screen      : QScreen       # the monitor everything positions against
```

Transitions:

- pick a mode (menu row or shortcut) → `mode` set, `menu = None`, `phase = 'armed'`;
  if `mode in IMMEDIATE_MODES`, fire the grab instead of arming
- pick a destination or delay → value set, `menu = None`, `phase` unchanged
- click the tab / press `Space` → `phase = 'choosing'`
- `Esc` → close `menu` if open, else cancel the whole snip
- selection completed → chooser destroyed, floating bar shown, `mode` handed over

No data fetching. The only I/O is reading Settings on launch.

---

## Qt notes

- **`backdrop-filter: blur(16px)`** has no Qt equivalent. The desktop behind is a static
  grab, so blur the panel's region of it once, cache it, and paint that crop as the widget's
  background before the fill. Cheaper fallback: raise fill alpha to ~0.97 and skip the blur.
  Never a live blur.
- **Square-top / rounded-bottom** — `QPainterPath` with per-corner radii, or
  `addRoundedRect` on a rect extended 14px above the visible top so the top corners clip off.
- **Alpha is not opacity.** The panel is a 93%-alpha *fill*; its children are fully opaque.
  Setting `windowOpacity = 0.93` washes out the icons. The one place opacity is genuinely
  correct is the armed tab's 0.72 — that whole widget really is translucent, so
  `QGraphicsOpacityEffect` (animated to 1.0 on `enterEvent`) is right there.
- **Dropdown menus** → `QMenu` will not accept this styling cleanly; build a frameless
  `Qt.Popup` `QWidget` with custom-painted rows. Two-line rows in the destination menu need
  a custom `sizeHint`.
- **Shadows** → `QGraphicsDropShadowEffect`, parameters in `tokens.Shadow`.
- **Wayland** — the chooser is a layer-shell surface anchored `top` on the target output when
  `wlr-layer-shell` is available; otherwise a frameless always-on-top window positioned as
  above. Test both: X11 lets you position freely, Wayland does not, and this is where the
  monitor-vs-desktop bugs will resurface.
- **Structure** → one frameless `QWidget` for the panel with real child widgets for the three
  triggers (so tooltips, hover and focus come free), plus separate popup widgets for the
  menus, the hint pill, the tab and the legend. The scrim is painted by the overlay window
  underneath, not by the chooser.

## Files

```
design_handoff_snipux_chooser/
├── README.md                        this document
├── tokens_chooser.py                chooser metrics, colours, mode hints — merge into tokens.py
├── icons/*.svg                      the 10 glyphs this surface uses, 24×24, currentColor
└── reference/
    ├── Snipux Chooser.dc.html       THE SPEC — interactive
    ├── Snipux Capture Bar.dc.html   the five rejected directions, with reasoning
    ├── Icon.dc.html
    └── support.js
```

Everything else — the overlay palette, `Metric`, `Shadow`, `CAPTURE_MODES`, `AFTER_CAPTURE`,
`DELAYS`, the full icon set — comes from `design_handoff_snipux/`.

## Open questions

1. **Does the hint line survive first use?** It is the replacement for the primary button, so
   it earns its place early; after fifty snips it may be noise. Suggest tying it to the same
   preference as the overlay's hint bar rather than adding a second toggle.
2. **Should `Esc` from armed return to choosing, or cancel outright?** Currently it cancels.
   A two-stage Esc would match what I suggested for the overlay — worth deciding once for
   both surfaces.
3. **Freeform has no design yet** beyond appearing in this list. The lasso interaction, how
   the scrim inverts against a path, and how export crops to the bounding box are all
   unspecified.
