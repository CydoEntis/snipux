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

## 7 · Crop was a fifth shapes sibling, until it went

**The handoff's** shapes family is four: rectangle, ellipse, straight line and
arrow (`SHAPES`, `R O L A`). Crop is not in `ANNOTATION_TOOLS` at all.

**We** kept Crop as a fifth sibling when the bar was rebuilt, and have since
taken it out (#78). The family is the handoff's four again, so this is no
longer a divergence; the entry stays so the numbering does.

### Why it was kept

The owner asked for all eleven tools to stay reachable (SNX-64), which
already outranks the eight-slot rule once (`../flow/divergences.md` §7). The
handoff's own answer for a tool that does not fit is a sibling, so that is
where it went, and the slot count did not move.

### Why it went

- **It cropped nothing.** Crop really cut the image in the retired editor
  window. What stayed was only its dashed box, placed as a mark, so a tool
  named Crop left the snip exactly as big as it was. Its owner found it in
  the menu and reported it as feeling broken.
- **Two things already do what it named or drew.** The selection's handles
  are what crop a snip, and Rectangle with a dashed line (#65) draws the same
  box.

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

## 9 · A bar with no room stays on its monitor, and can be dragged off it

**The handoff's** only answer for a bar that does not fit is the clamp in §8.

**We make the bar draggable** (#50), onto another monitor too, and remember
where it is dragged to when the selection leaves it no room. There is no key
to hold it out of the way. For a while the bar also went to another monitor
on its own; #79 took that back.

### Why

No room above or below means the selection is essentially the whole monitor,
so any position on that monitor covers something. The owner decided on #50
that both halves are needed, because they cover different cases. Another
monitor handles the common case, with nothing to do by hand. A drag is the
answer on a single monitor, where there is nowhere to send the bar, and the
escape hatch when the automatic choice is wrong. That is #63's "another
monitor, or wherever the user drags it", with the order between the two
settled. The hold-to-hide key is dropped.

In use, the automatic move put the controls a monitor away from the selection
they were for, pinned to the bottom of that monitor beside the bezel, and it
was reported as bad UX (#79). The bar now stays at the foot of the selection
on its own monitor, where the eye already is. A drag is how it gets out of
the way, onto another monitor if that is where it is wanted, and it is
remembered from then on.

#50 lands after the stills bar (#67), so its placement rules are written once,
against the new bar.

### How it behaves

In priority order:

1. **Room beside the selection always wins.** Below it, then above it, as §8
   says. A drag there moves the bar for that selection only, keeps it on the
   selection's monitor and is not remembered. Neither a remembered place nor
   another monitor can put the bar over a selection that had room.
2. **With no room,** the bar sits inside the selection's own monitor against
   its bottom margin, centred on the selection, on a desk with any number of
   monitors -- including Wayland, where the overlay covers only one output.
   Until #79, a desk with another monitor sent the bar there instead.
3. **A drag with no room can carry the bar onto another monitor**: the
   nearest one that none of the selection is on and that can hold the bar,
   nearest measured between monitor centres, the rule the recording bar
   already uses.
4. **A drag with no room is remembered, and wins over 2 next time**, so
   the automatic choice and the user's correction of it never take turns. The
   drag can carry the bar between the selection's monitor and the other one.
   What is remembered is which of the two, relative to the selection's, and
   where on it. A place remembered on the other monitor, on a desk that now
   has none, keeps its spot on the selection's own.

Whichever monitor the bar is on, the bar and everything hung off it -- the
family menus, the style popover, the tool hint, the capture popover -- stay
inside that monitor's usable area, clear of its top bar and dock, and never in
the gap between monitors. The close button, the toast and the chooser's tab
belong to the capture rather than the bar, so they stay on the capture's
monitor (#49, and §18 for the tab).

- **It is taken hold of by its own surface**: the padding round the row, the
  gaps between controls and the dividers, under an open hand. A press on a
  control stays that control's, and a press that moves less than the
  platform's drag distance moves nothing.
- **A remembered place is stored** as `{"monitor": "own" | "other", "spot":
  [x, y]}`. The spot is a fraction of the room the bar can travel inside that
  monitor's usable area, so it names the same place on a monitor of any size
  and stays clear of the top bar and dock. A bare `[x, y]`, saved before the
  bar could change monitor, reads as a place on the selection's own monitor,
  the only one it could have been dragged on. Anything unreadable in the
  config means automatic placement.
- **A drag cannot take the bar to a third monitor.** What is remembered is
  "the selection's monitor" or "the other one", so a drag toward any other
  monitor stops at the nearer of those two.
- **Chrome is never in the export**, whichever monitor it sits on.
- **The review window's bar does not drag.** It sits at the canvas floor,
  which the fitted image keeps a margin from, and zooming out clears it.

---

## 10 · The watermark is text or an image

**The handoff** leaves what the mark is ("logo, text, colour") to Settings
(`WATERMARK["content_lives_in"]`), and no Settings design covers it.

**Ours is text or an image**, and Settings edits both on a Watermark page of
its own (#69). A text mark is the handoff's placeholder chip: light type on
a dark plate, in a colour the user does not pick. An image is drawn as it
is. Either is sized to the snip -- 5% of its shorter side, between 20 and 64
logical pixels tall -- and an image never past its own pixels. An image is
copied into the config folder rather than remembered by path. The review
window's bar has no watermark slot.

### Why

The owner's decision on #69, which closes #63's open question 2. The plate
is there because a watermark lands on whatever the capture holds in its
corner, light or dark. A copy survives the original being moved or tidied
away, where a path does not. And the snip a review window opens was already
exported by the overlay, stamped if the watermark was on, so a slot there
could only stamp it twice.

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

The chooser's mode menu is the exception, and is a top-level popup as
the handoff says (#66): it has to paint over the hint pill under the row,
and the only keys it keeps from the window while it is open are the mode
letters its own rows offer.

---

## 15 · Five details of the style popover

**The handoff** gives `STYLE_SECTIONS` and `DEFAULT_STYLE` for the tools it
has, keeps one `strength` in its state model, steps only the stroke with `[`
and `]`, and rings the picked swatch.

**We** keep a strength per redaction tool and let `[` and `]` step it, draw
a redaction's style dot in the bar's grey, draw the picked swatch's ring
inside the swatch, and hide the tool hint while the popover is open. Crop had
a colour and a stroke of its own too, until it was taken out (§7).

### Why

- **A strength per tool:** the same number is a light smudge to blur and
  coarse blocks to pixelate (`marks.ToolStyles`).
- **`[` and `]` on a redaction:** strength is the one length a redaction's
  popover offers, so the stepping keys step it.
- **The grey dot:** a redaction has no colour for the dot to preview.
- **The ring inside:** each swatch is its own widget, and a child widget
  cannot paint outside its own rect.
- **The hidden hint:** where the bar has no room below, the hint and the
  popover would both open above it, one on top of the other, and the lit
  slot already says which tool the popover is styling.

---

## 16 · The chooser is not 382px wide

**The handoff says** the chooser is "382 × 42" (`BarMetric.CHOOSER_W`),
measured with Region chosen and set in IBM Plex Sans.

**Ours is its metrics plus its measured label**: 246px plus the width of
the mode's name, about 285 with Region in Plex. Tests compute that sum and
never assert 382.

### Why

It is what the spec's own markup adds up to: 1px border and 6px padding
either side, the kind well (62), the mode chip (56 plus its label), the
divider (9), the destination (28) and the flag well (65), with 3px between
the five. Nothing in the spec reaches 382. The README says an earlier draft
ended the row with a `Pick a window` button, and a button that size makes up
the difference, so the figure most likely predates its removal. Plex is also
not bundled (see Still open, "Fonts"), so any fixed width would be wrong in
the face that actually resolves.

---

## 17 · Last region opens a snip, and works on the record side

**The handoff** makes Last region a row of the mode menu that restores the
previous capture's rectangle. It says nothing of a stored preference, and
its state model persists the mode only within a session.

**Ours keep the stored preference.** Before #66 Last region was a toggle on
the row meaning "open on the last region", saved as `reuse_last_region`.
Choosing the mode is that choice now: picking Last region writes it on,
picking any other mode writes it off, and a snip opens on the rectangle
while it is on -- offered, not taken, as the toggle did. Choosing Last
region during a snip takes the rectangle, so `instant` finishes on it, and
on the record side it arms the ready stage the way a clicked window does.
With nothing captured the row is greyed with "Nothing captured yet"; with a
rectangle on a monitor that is not here, "Not on these monitors".

### Why

#66 keeps what persists across restarts as it was, and someone who had the
toggle on must not lose their rectangle to the redesign. The record side
takes it because it resolves to a rectangle before anything is filmed, the
reason Window and Active window are offered there
(`tokens.RECORD_DISABLED_MODES`). Opening on it stays stills-only, since
opening a recording on a rectangle would arm the recording.

---

## 18 · The tab reopens the row over the selection

**The spec's** `reopen` returns the snip to its choose stage: the stills
bar goes, and the selection stays drawn until a new one replaces it.

**Ours reopen the row over a live selection.** The bar, the marks and the
selection stay as they are, and the row folds back to its tab on Space,
Escape, a press on the frame or picking a tool. While a recording is armed
the chooser is not shown at all.

### Why

In this build a mark lives in window coordinates and a selection can only be
redrawn by hand, so a reopen that dropped the bar would leave no way back to
the annotated selection short of dragging it again. A recording that is
armed is state `app.py` holds, and a mode picked from the row would pull the
region out from under it.

---

## 19 · Hide sensitive is not on the record side

**The handoff** draws the flag well, Hide sensitive and Delay, on the row
whichever kind is chosen.

**Ours leave only Delay** on the record side.

### Why

Hide sensitive reads text out of a frozen frame, and a recording has no
frozen frame to read. A flag that could not do anything would look live.

---

## 20 · Smaller departures in the row

- **The wells are 32px, centred in a 42px row.** The spec pads a 28px
  button 2px inside a well and the well 6px inside the row, which is 45px;
  #66 fixes the row at `ROW_H`.
- **A hovered control also explains itself in the hint pill.** Tooltips are
  set as the spec's `title`s are, but Qt's tooltips are unreliable on an
  always-on-top frameless window.
- **The tab's opacity changes on hover without the 160ms ease.**

---

## 21 · The style popover is roomier than 216px

**The handoff says**: 216px wide, 9px and 10px padding, 21px swatches 4px
apart, 30x24 fill and line buttons, a 13px slider thumb, and a slider that
takes whatever is left of the row.

**We make it 264px wide**, with 12px padding, 24px swatches 6px apart, 36x28
fill and line buttons, a 15px thumb and 12px between the two rows. Beside the
fill and line buttons the stroke slider gets about 110px instead of 83. The
sizes are `BarMetric`'s style-popover tokens.

### Why

Built to the handoff's sizes it was reported as squished: the swatches all
but touched, the fill and line buttons were small targets, and a stroke of 1
to 26 across 83px of slider is barely 3px a step. The structure stays the
handoff's -- the colour row, then fill, line and the slider with its readout,
and only the sections a tool supports -- and only the sizes grow.

---

## 22 · What the glass blurs, how far, and where it does not

**The handoff says** (`README.md`, "Qt notes"): `backdrop-filter` has no Qt
equivalent. The desktop behind is a static grab, so blur each bar's region
once, cache it and paint that crop behind the fill; where that cannot be
had, raise the fill alpha to about 0.97 and skip the blur; never a live
blur.

**We** blur the frozen frame as captured, 16px on every surface, and keep a
moving surface's crop from where it last rested until it rests again. The
fallback raises a fill to 97% only where the fill is thinner, and the
review window's bar keeps its own fill (#70, `snipux/glass.py`).

### Why

- **The frame, not the scrim over it.** A CSS backdrop filter blurs
  whatever is painted behind the element, which outside the selection is
  the frame under the 62% scrim. A crop of the frame alone holds for the
  whole snip. One with the scrim in it would change whenever a re-frame of
  the selection passed under a bar, and would have to be taken again each
  time. The frame shows through a 94% fill at 6%, so over white the bar
  reads up to about ten levels lighter than it would over the dimmed frame.
- **16px everywhere.** The bars handoff names the filter without a radius.
  16px is the flow handoff's figure for a bar, and the overlay redesign's;
  one radius keeps a menu on the same glass as the bar it opens from.
- **A moving surface keeps its last crop.** A dragged bar (§9), or a bar
  following a selection being re-framed, moves on every mouse event, and a
  crop for each move is a live blur. The crop from where the surface set
  off stands in until it has stayed put for 150ms, and then one is taken
  there: two blurs a drag. Under the fill, the stale crop does not show
  while the surface moves.
- **The fallback raises a fill, never lowers one.** A menu's fill is
  already 98%, and stays 98% where there is no blur.
- **The review window's bar keeps its fill, with no blur and no fallback.**
  It sits over the review canvas, not a frozen frame, and the canvas
  changes under it with every zoom and stroke: a cached crop would be stale
  by the next change, and a fresh one each time is a live blur. At the fit
  the window opens on, what is behind the bar is the workspace gradient,
  which a blur would leave as it is.

---

## 23 · A click on an armed family slot opens its menu

**The handoff says** (`README.md`, "Notch families"): the slot shows
whichever sibling you used last, and the corner triangle opens the family.
Its markup draws the triangle in a 9px box, 1px in from the slot's corner.

**We** also open a family from a click on its slot while the sibling the
slot shows is armed, and close it on the next click. The triangle is drawn
where the handoff puts it, but the notch answers to a 12px corner around it
(#77). The watermark's notch grows the same way.

### Why

- **The triangle was the only way in with the pointer, and it was
  missed.** It was reported as having to click in exactly the right place
  for anything to open. A press a few pixels off the 9px box landed on the
  slot, which armed the shape it already had, so nothing seemed to happen.
- **A click on an armed slot had nothing else to do.** A click arms the
  sibling the slot shows and never the next one, so a second click changed
  nothing. Opening the menu gives it a use without making the first click,
  the one used most, any less predictable.
- **12px, not more.** Nearly twice the area of the 9px box, and still short
  of the middle of a 28px slot, so a click aimed at the glyph arms the tool
  rather than opening the menu over it.
- **Right-click and the keys are unchanged**, and so is the watermark
  slot's own click: it switches the mark, having no sibling to arm.

---

## 24 · The highlighter snaps to text

**The handoff's** highlighter is a wide, see-through freehand stroke, and its
`STYLE_SECTIONS` give it colour and stroke.

**We** fit a highlighter sweep to the lines of text under it when it is
released: one clean band per line, from the first word the sweep touched to
the last. It is on by default, and a third click-through in the style popover,
between colour and stroke, switches the tool back to freehand for the session.
A sweep over no text, or over a photo, stays as drawn either way.

Its first-run colour is yellow, `#facc15`, not the handoff's amber `#f59e0b`.

### Why

- **A hand-drawn highlight never sits on the line.** It starts a letter late,
  rides up into the line above and stops short of the last word -- reported
  with a screenshot of exactly that.
- **Pixels, not text recognition.** The frame is already in memory, so finding
  ink on a flat background runs the same on Linux and Windows and needs
  nothing from the OS. Windows' text recognition would have made it a
  Windows-only feature (`snipux/textsnap.py`).
- **Several lines, both ways people draw them.** A swipe per line keeps each
  line's own words; one diagonal drag reads as an editor's drag selection.
- **Yellow, because it is a highlighter.** Amber at the highlighter's
  see-through alpha was reported as orange. Yellow is a default, not a
  swatch: an eighth swatch would widen the popover and take a number key the
  row does not have.
- **A click-through, not a key held while drawing.** It is a choice about the
  tool, like fill and line, and lives with them, in the same session-long
  style that is never written to disk.

---

## 25 · Record is a camcorder, and the side is never remembered

**The handoff** draws the record side of the kind well as "a filled 10px
circle, not a glyph" (`README.md`, "Kind"), and its state table carries
`kind: 'stills' | 'record'` as remembered between snips, which is how we
built it.

**We** draw a camcorder glyph beside the camera, and open every snip on
stills. Flipping to record lasts for that snip alone.

### Why

- **A dot beside a camera says "on", not "a video".** The pair reads as one
  choice between two things, so both sides say what they make.
- **A remembered side films by accident.** The next snip after a recording
  opened armed to record, a hotkey and a drag away from filming when a
  screenshot was wanted -- reported exactly that way. Recording is the rarer
  and the more surprising of the two, so it is the one chosen on purpose.
- **Nothing else about the row is lost.** The destination, the delay and
  Hide sensitive are still remembered; only which side of the well is not.

---

## 26 · Copy text grows the stills bar past 478px

**The handoff's** "Eight slots, no more" (`README.md`, rule 2) answers every
new *tool* with a sibling, a popover section, or a Settings preference, and
names the stills bar's own width, "478 × 42", as fixed.

**We add a ninth control**, Copy text (#82), beside the split action, past
its own divider -- and the bar grows by its width to carry it.

### Why

Rule 2 is about the seven tool slots -- the pen through the eraser, the
gradient of consequence -- and the handoff's own examples (a rounded-rect
sibling, an arrow-head popover, a default-ink preference) are all about
*drawing*. Copy text draws nothing; it ends the snip the way Copy does, so
it belongs with the split action, not the tools. It could not join the
destination caret either -- the caret's three are `AFTER_CAPTURE`, a
persisted default, and reading text out of one selection is not a way every
future snip should end (see `FloatingBar`'s own docstring). With nowhere
inside the existing 478px to put a control that is neither a tool nor a
destination, the row is wider instead.

`tests/test_glass.py`'s `TestTheOverlayIsTheHost` widened its left monitor
to match (700px, not 600): the destination menu overhangs the bar's own
left edge, centred as it is on the split action near it, and the wider bar
left it just short of that monitor's edge.

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
