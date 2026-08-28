# Divergences from the locked capture-flow handoff

`README.md` in this directory is marked **LOCKED**. These are the points we
build differently anyway, each with the reason, so nobody reads the handoff
later and "fixes" the code back to it. Same purpose as
`../pre-snip-chooser.md` serves for the chooser handoff.

Everything not listed here is built as written.

---

## 1 · Copy does not write a file

**The handoff says** (`tokens_flow.ALWAYS_WRITE_FILE = True`):

> All three write the file first — Copy and Open included. A capture that
> exists only on a clipboard is one Ctrl+C from gone.

**We do not,** for Copy. Copy puts the capture on the clipboard and leaves
no file in the save folder. Save and Open still write.

### Why

The handoff is answering a real complaint — "every bug report starts with
'where did it go'" — and it is the same complaint that reached us:

> idk where it saved it or if it did looks likt its in my clip board but
> its hard to know that

But the cause was not that the file was missing. It was that the file
existed when the user had not asked for one. `_land_recording()` moved
every recording into the save folder and only *then* looked at the
destination, so "copy to the clipboard" also silently saved, into a folder
the user was never shown, under the stills filename pattern. The two
destinations described themselves as alternatives and were not.

So we fixed it the other way round: the destination you pick is the only
thing that happens, and the toast says what happened. "Where did it go" is
answered by the toast naming the destination, not by always writing a file
in case.

Always-write also has a cost the handoff does not weigh: a user who only
ever copies accumulates a folder of files they never asked for and will
not think to clean up.

### The one place this leaks

A recording copied to the clipboard **does** leave its temp file on disk,
because `copy_file_to_clipboard()` puts a *reference* there rather than the
video's bytes -- deleting it would hand the user a clipboard entry that
pastes nothing. That file lives in the recording temp dir and is swept at
the next startup, not in the save folder. "No file is kept" means none
where the user keeps things.

Stills are unaffected: an image goes on the clipboard as image data, so
there is nothing to keep.

---

## 2 · Audio is Windows-only, and says so

**The handoff** gives every recording bar an audio dropdown —
`AUDIO_SOURCES`: System, Mic, Muted — with no platform caveat.

**We build the control as designed**, and on Linux offer only *Muted*,
with the other two disabled and carrying the reason.

### Why

`org.gnome.Shell.Screencast` has no audio at all: there is no option to
pass and nothing to wire a control to. Audio on Linux would mean capturing
from PipeWire ourselves and muxing it, which is a different piece of work
from this redesign and would pull in a dependency the project has so far
refused (CLAUDE.md: adding a fourth is a decision worth raising in the
ticket).

A control that is visible and does nothing is the failure mode this
handoff already names elsewhere — "a control that opens a menu it can't act
on is a lie" — so the options that cannot work are disabled and say why,
exactly as `RECORD_DISABLED_MODES` already does for Window and Freeform on
the record side.

Windows gets all three.

---

## 3 · Not built yet, and why

Listed here rather than silently skipped. None are refusals; all three are
things the handoff itself leaves unresolved.

- **Pause/resume** — the handoff's own "Still open" #2 does not say whether
  a paused recording is one continuous file or segments concatenated on
  stop. `QMediaRecorder` can pause on Windows; GNOME's screencast cannot,
  so on Linux it would have to be stop-and-restart, which is precisely the
  semantics question left open. Needs deciding before it is built.
- **"Open" for a recording** — specified as "player with trim, mute and GIF
  export", which the handoff's "Still open" #3 says is described but not
  designed. Trim is separately deferred (`recording.md`: v1 records, it
  does not edit). Until it exists, Open on the record side is not offered.
- **Freeform** — "Still open" #1 says it has no interaction design and
  behaves as a region drag in the prototype. It stays as it is today.

---

## 4 · The overlay cannot stay up while recording

**The handoff says** (§5): the live stage keeps the same surface, with the
scrim dropping *"from 62% to 28% so the user can see what they're filming"*,
and the selection frame switching to solid red around it.

**We take the overlay down the moment recording starts.** It stays up for
stage 3b (ready), where the resize handles are live and the frame and scrim
are exactly as designed; it closes as the countdown ends.

### Why

The overlay paints a **frozen** capture across its whole surface --
`drawImage(rect, self._base_layer_image())` in `overlay.py` -- including
inside the selection. That is the project's one architectural rule working
as intended: grab the entire virtual desktop once, then run everything
downstream against that still frame.

So there is nothing to see through. Dropping the scrim to 28% over a frozen
frame does not reveal what is being filmed; it reveals a dimmer photograph
of the moment the snip started. Worse, `org.gnome.Shell.Screencast` records
the *composited screen*, so an overlay left up would put that photograph
into the file -- the recording would be a still image of the desktop with a
scrim over it.

The prototype does not hit this because its desktop is a static mock: a
frozen frame and a live one look identical there, which is exactly the class
of thing a running prototype cannot tell you.

### What it would take

A real hole -- `CompositionMode_Clear` over the selection on a translucent
window, plus an input mask so clicks still reach the desktop underneath.
That is feasible on a compositing desktop and unverified on Wayland, which
this project has never run on at all (TODO.md). It is a piece of work in its
own right, not a detail of this redesign, and it trades against the one rule
CLAUDE.md says is not negotiable.

### What this costs

The scrim drop, the red frame and the live dimension chip are all stage-5
chrome on a surface that no longer exists by stage 5. The bar carries the
whole live state instead: red border, red clock, and Stop.

It also takes rule 1's other half with it. The handoff centres every
post-selection bar **on the selection, 16px below** -- which reads as
attached to the region only while the region is still drawn, scrimmed and
framed around it. Once the overlay goes, a bar below a mid-screen region is
a lone pill floating in the middle of the screen with nothing to belong to,
which is a real complaint this project already had and already fixed:

> The HUD floats in the middle of the screen. It should sit at the top, the
> way the chooser and floating bar do.

So the bar is **top-centre of the monitor being recorded**, moved below the
region only when the region itself covers that strip. That keeps it
predictable (always the same place, whatever was selected), keeps it out of
the frame, and keeps it in one place across every stage -- which is what
rule 1 was protecting. The rule survives; the coordinate it was measured
from does not.

---

## 5 · Eleven tools in the stills bar, not eight

**The handoff says** (§3a): *"split action button → divider → 8 tools →
divider → undo, clear ink"*, and `ANNOTATION_TOOLS` lists exactly eight.

**We ship eleven.** Ellipse, Line and Crop stay.

### Why

This one is older than this handoff and already recorded against the
previous one -- see the note on `TOOLS` in `design/tokens.py`. `shapes.py`
has always fully implemented all three; the redesigned chrome simply stopped
naming them, so the bar offered eight of the eleven the owner had asked --
before any redesign started -- to keep, having tried them and found they
worked. That instruction outranks a handoff's tool count, the same way the
16x16 minimum selection does.

They are not three more bar buttons: the handoff's own guidance for a tool
that does not fit the eight is a submenu off an existing button, and that is
where they live -- behind the rect button's popover (`ShapeToolPopover`).
So the bar still reads as eight, which is what the rule was protecting.

### What this means for the token merge

`ANNOTATION_TOOLS` is not merged. Everything in it already exists here as
`TOOLS` + `SHORTCUTS` + `TOOL_HINTS`, covering eleven tools instead of
eight; adopting the handoff's list would delete three of them. Same for
`CAPTURE_MODES`, for a different reason: its shortcut letter and next-step
hint are already `MODE_KEYS` and `MODE_NEXT_STEP`, the latter word for word.

---

## 6 · No IBM Plex

**The handoff says** the chrome is IBM Plex Sans and every numeral, dimension,
clock, size and shortcut is IBM Plex Mono, both shipped with the app, and that
"sizes are fixed and the layout is tuned to them".

**We use the fonts already in use** — whatever `_ui_font()`/`_mono_font()`
resolve to on the platform. `design/fonts/` stays empty.

### Why

Vendoring two font families is a licensing, packaging and bundle-size task
that has nothing to do with the flow being redesigned, and it would land in
every build (PyInstaller bundle included) before a single bar was rebuilt.
Called explicitly, by the person who has to look at it: *"u dont need to
match the fonts if its a pain in the ass"*.

### What this costs, so it is not a surprise later

The handoff's fixed sizes are tuned to Plex's metrics. A substituted font
has different advance widths, so anything sized to fit exact text -- the
clock, the dimension chip, the menu widths in `FlowMetric` -- must be
measured with `QFontMetrics` rather than trusted to the token. There is
already one test skipped for exactly this reason (`test_overlay.py`, the
nav-rail budget measured against a fallback face), so the failure mode is
known: text overruns a panel that the token said would hold it.

Build to the tokens, but let anything text-sized grow to its own
`sizeHint()` rather than pinning it.
