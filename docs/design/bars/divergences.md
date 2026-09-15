# Divergences from the locked bars handoff

`README.md` in this directory is marked **LOCKED**, and replaces the chooser
and stills-bar material in `../flow/` and `../pre-snip-chooser.md`. These are
the points we build differently anyway, each with the reason, so nobody reads
the handoff later and "fixes" the code back to it. Where the handoff and an
open issue disagree, the issue wins: each was decided on its own, usually
after a user report.

Everything not listed here is built as written. What is not decided yet is
under **Still open** at the end, and today's behaviour stands for each.

---

## 1 · Freeform is removed, not rebuilt

**The handoff lists it**: a row in `CAPTURE_MODES`, `L` in `SHORTCUTS`
("mode Freeform / shape Line"), and "Still open" 1, which says it has no
interaction design and behaves as a region drag.

**We remove it** (#42). `L` is Straight line on the stills bar and nothing in
the chooser.

### Why

The handoff leaves Freeform's design open, and #42 is the answer to that. It
lands before either bar, so neither is rebuilt around a mode that is leaving.

---

## 2 · Browser stays in the mode menu, and Active window joins it

**The handoff's** `CAPTURE_MODES` is Region, Window, Full screen and Freeform.
There is no Browser row.

**We keep Browser** as a row in the mode menu. Active window (#43) joins it
as another row.

### Why

Browser ships today, and greys itself with its reason wherever it cannot
work. Active window captures the focused app with nothing to aim at: it
answers the same question, what to capture, so it is a row beside the others
rather than a new control.

---

## 3 · Full screen arms on a desk with more than one monitor

**The handoff says** (`IMMEDIATE_MODES = ["Full screen"]`): "`Full screen` is
the exception: nothing left to aim at, so it captures immediately."

**We capture the moment it is picked only on a desk with one monitor.** With
more than one, picking it arms it like the other modes, and it follows the
pointer before it commits (#53).

### Why

"Nothing left to aim at" is only true with one monitor. With two, which
monitor is still a choice, and capturing on the pick takes whichever one the
chooser happened to open on.

---

## 4 · Destinations do not change

**The handoff's** `DESTINATIONS` are Copy, Save and Edit, and the chooser's
destination icon cycles Copy → Save → Edit.

**Ours stay as they are.** The destination icon cycles through today's stills
destinations (`tokens.AFTER_CAPTURE`).

### Why

The handoff itself says destination semantics are unchanged and still come
from the flow handoff, and `../flow/divergences.md` already records how ours
differ from that.

---

## 5 · Hide sensitive blacks text out; it does not blur it

**The handoff's** tooltip (`FLAGS`): "Blurs password fields, tokens and card
numbers on capture."

**Ours blacks it out**, and the tooltip keeps `tokens.HIDE_SENSITIVE_HINT`'s
wording: "Black out passwords, keys, cards and personal info". The glyph is
the handoff's `eyeOff`.

### Why

That tooltip describes a feature we do not ship. Someone deciding whether to
trust it needs to know what it does, and here the difference matters: the
handoff's own redaction notes say blur "Softens it — shapes still readable",
where blackout leaves "Nothing to reconstruct".

---

## 6 · The minimum selection stays 16 x 16

**The handoff says** a drag under 80 × 50 is discarded (`BarMetric.MIN_SEL_W`
/ `MIN_SEL_H`, and `sel` in the state model).

**Ours stays 16 x 16** (`tokens.Metric.SEL_MIN_W` / `SEL_MIN_H`), the minimum
`../flow/divergences.md` §7 already records as outranking a handoff.

### Why

SNX-33 set that floor so a taskbar icon or a single line of text can still be
snipped, and 80 × 50 would discard both.

`tokens.FlowMetric.MIN_SEL_W` / `MIN_SEL_H` (60 x 40, the flow handoff's
figure) is still in `tokens.py`, but nothing reads it: the overlay enforces
`Metric.SEL_MIN_W` / `SEL_MIN_H`.

---

## 7 · Crop stays, as a fifth shapes sibling

**The handoff's** shapes family is four: rectangle, ellipse, straight line and
arrow (`SHAPES`, `R O L A`). Crop is not in `ANNOTATION_TOOLS` at all.

**We keep Crop**, as a fifth sibling in the shapes family.

### Why

The owner asked for all eleven tools to stay reachable (SNX-64; the
siblings are `tokens.SHAPES`), which already outranks the eight-slot rule once
(`../flow/divergences.md` §7). The handoff's own answer for a tool that does
not fit is a sibling, so that is where it goes, and the slot count does not
move.

---

## 8 · The stills bar goes above the selection when there is no room below

**The handoff says**: centred on the selection, 16px below it, clamped 12px
from any monitor edge and to `monitor_h − 108` (`BAR_OFFSET_Y`,
`BAR_EDGE_MARGIN`, `BAR_BOTTOM_ROOM`).

**We centre it 16px below, as written, when it fits.** When it does not, it
goes above the selection instead of being clamped back up over it.

### Why

The clamp puts the bar over the pixels the user just framed in order to mark
them up. That is the report `FloatingBar.reposition`'s docstring and comments
in `snipux/overlay.py` record: "when u select a small region the controls are
in the region so u cant edit anything", on a 1123x74 strip. A short selection
is the usual way to hit it, but distance to the monitor's bottom edge is what
decides it.

---

## 9 · A bar with no room anywhere can be dragged, and remembers where

**The handoff's** only answer for a bar that does not fit is the clamp in §8.

**We make the bar draggable** (#50), and it remembers where it was put. It is
not moved to another monitor, and there is no key to hold it out of the way.

### Why

No room above or below means the selection is essentially the whole monitor,
so any position the code picks covers something. The owner chose drag on #50,
over another monitor or a hold-to-hide key. This supersedes #63's "another
monitor, or wherever the user drags it": it is drag only.

#50 lands after the stills bar (#67), so its placement rules are written once,
against the new bar.

### How it behaves

- **It is taken hold of by its own surface**: the padding round the row, the
  gaps between controls and the dividers, under an open hand. A press on a
  control stays that control's, and a press that moves less than the
  platform's drag distance moves nothing.
- **Room beside the selection still decides first.** Below it, then above
  it, as §8 says. A drag there moves the bar for that selection only and is
  not remembered, so a remembered place can never put the bar over a
  selection that had room.
- **With no room on either side** the bar goes where it was last dragged to
  in that case. It is stored as a fraction of the room the bar can travel
  inside the monitor's usable area, so it names the same place on a monitor
  of any size and stays clear of the top bar and dock. Anything unreadable
  in the config is today's placement.
- **The review window's bar does not drag.** It sits at the canvas floor,
  which the fitted image keeps a margin from, and zooming out clears it.

---

## 10 · The watermark is text or an image

**The handoff** leaves what the mark is ("logo, text, colour") to Settings
(`WATERMARK["content_lives_in"]`), and no Settings design covers it.

**Ours is text or an image**, and Settings will edit both (#69).

### Why

The owner's decision on #69, which closes #63's open question 2.

---

## 11 · PyQt6 only

**The handoff targets** "Python + Qt (PySide6 / PyQt6)".

**This repo is PyQt6 only.**

### Why

The dependencies are PyQt6, jeepney and pytest (`CLAUDE.md`), and adding one
is a decision for its own ticket, not a detail of a design.

---

## 12 · Three shared glyphs keep the repo's drawing

**The handoff's** `icons/` holds 28 glyphs, each with an embedded metadata
block. Its README says 27, with `eyeOff`, `blackout` and `mask` new; `layers`,
the watermark's glyph, is new too.

**`snipux/design/icons/` takes the four new ones** with the metadata block
stripped, the plain form every icon there already has. Of the 24 glyphs both
sets share, 21 are identical once stripped. `ellipse`, `highlighter` and
`line` differ, and ours stay.

### Why

The repo's three were deliberately re-centred in their 24x24 box: the artwork
is the designer's, only its placement was wrong. The comment inside
`highlighter.svg` gives the measurements.

---

## 13 · The accent stays the softer green

**The handoff says**: `ACCENT` is `#e3ff4f`, with `ACCENT_SOFT` `#eaff7a` for
accent text and small glyphs.

**We keep `tokens.Color.ACCENT` at `#a8e05f`**, with `#c3e399` as its lighter
variant. The "Acid" ink swatch is untouched.

### Why

The full-saturation yellow-green read as neon against the dark chrome --
"not so vibrant" was the report -- and on a switch or a Save pill it pulled
the eye harder than the thing it was labelling. `#a8e05f` is sampled from the
app icon's selection marquee, so the accent and the mark are the same green;
the comment on `Color.ACCENT` gives the history. Acid is a drawing colour,
not chrome: changing it would repaint what the pen puts on the image rather
than what the interface looks like.

---

## 14 · Menus are children of the window the bar sits over

**The handoff says**: menus are top-level popups, not children of a bar, so
an open menu paints above the hint pill beneath it and no effect-bearing
parent can trap it.

**We make them children of the overlay, or of the review window** -- not of
the bar, and not top-level popups (`FamilyMenu` in `snipux/overlay.py`). As
children of the window they still paint above the bar and the strip under
it, which is what the handoff was guarding against.

### Why

A top-level popup takes the keyboard while it is open. A sibling's key
(`R O L A`, `B`) and Esc would stop reaching the window, and the handoff's
own condition for a one-row bar -- `1`-`7`, `[`, `]` and `D` working while the
style popover stays open -- could not be met. A press outside a popup is also
replayed underneath or swallowed depending on the platform. As a child, the
window's own press handler decides what a press outside the menu means --
close it, and nothing else -- and `_Chrome` keeps a press on the menu from
ever reaching that handler.

---

## 15 · Six details of the style popover

**The handoff** gives `STYLE_SECTIONS` and `DEFAULT_STYLE` for the tools it
has, keeps one `strength` in its state model, steps only the stroke with `[`
and `]`, and rings the picked swatch.

**We** keep a strength per redaction tool and let `[` and `]` step it, give
Crop a colour and a stroke, draw a redaction's style dot in the bar's grey,
draw the picked swatch's ring inside the swatch, and hide the tool hint while
the popover is open.

### Why

- **A strength per tool:** the same number is a light smudge to blur and
  coarse blocks to pixelate (`marks.ToolStyles`).
- **`[` and `]` on a redaction:** strength is the one length a redaction's
  popover offers, so the stepping keys step it.
- **Crop:** it is ours, not the handoff's (§7). Its dashes are its own, so it
  takes a colour and a stroke and no line style, seeded from what the spec
  gives every tool `DEFAULT_STYLE` leaves out (`DEFAULT_STYLE_OTHER`).
- **The grey dot:** a redaction has no colour for the dot to preview.
- **The ring inside:** each swatch is its own widget, and a child widget
  cannot paint outside its own rect.
- **The hidden hint:** where the bar has no room below, the hint and the
  popover would both open above it, one on top of the other, and the lit
  slot already says which tool the popover is styling.

---

## Still open

Not decided. Today's behaviour stands for each until it is, and each is
repeated on the child of #63 that it blocks.

### Fonts

**The handoff says** ship IBM Plex Sans for chrome and IBM Plex Mono for every
numeral, dimension, hex and shortcut: "sizes are fixed and the layout is tuned
to them."

**Today** (d3c13f9) Plex is not bundled. The app asks for it and falls back to
faces each platform ships -- Segoe UI Variable Text and Cascadia Mono on
Windows, SF Pro and SF Mono on macOS, Cantarell and DejaVu on Linux -- and Plex
wins only where it is installed.

**What it affects.** #66's 382px chooser (`BarMetric.CHOOSER_W`) is measured
in Plex, as is the stills bar's 478. A fallback face has different advance
widths, so text-sized controls must be measured rather than trusted to the
token (`../flow/divergences.md` §8). Bundling is a packaging decision (OFL, a
few hundred KB) rather than a dependency, but it is still a decision.

### Hint lines

**The handoff** leaves open ("Still open" 3) whether they show forever or
retire after some number of successful snips, and suggests tying them to the
overlay hint bar's preference rather than adding a second toggle.

**Today's behaviour stands** until that is decided.

### Custom colour

**The handoff's** `+` opens `QColorDialog`, and it leaves open ("Still open"
4) whether a picked colour joins the seven swatches for good or lasts only the
session.

**Today's behaviour stands** until that is decided.
