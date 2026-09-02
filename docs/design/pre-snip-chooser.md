# Divergence: the pre-snip chooser

`handoff-windows.md` and `overlay-redesign.md` both put capture mode on the
**floating bar**, reached through a chip that opens a popover.

We do not. The mode is offered **before anything is selected**, on a bar at
the top of the monitor the snip opened on.

## Why

The floating bar only exists once a selection does. So the only route to
"capture a window" or "capture the whole screen" was:

1. drag out a region you do not want,
2. click the chip on the bar that appears,
3. pick the mode you actually wanted,
4. watch the region you dragged get thrown away.

Picking what to capture is the *first* decision of a snip. Making it
reachable only after committing to a different one inverts that, and it read
as the mode picker being broken rather than merely awkward — it was reported
that way more than once before anyone worked out what it was doing.

## What it does

Two groups, because a snip is two decisions.

**What to capture** — `tokens.CAPTURE_MODES`, unchanged: Region, Window,
Full screen, Freeform.

Region gained a preference rather than a fifth mode, and the row gained a
fourth control to hold it: a `redo`-glyph toggle immediately after the mode
trigger (`chooser._ReuseToggle`). With it on, Region opens with the
previous snip's rectangle already framed and the toolbar up, so re-shooting
the same area needs no second drag — accept it, nudge an edge, or drag
anywhere outside it to frame something else. It is a *pre*-selection, never
a commit: nothing is captured because an overlay opened, `instant` does not
fire, and the record side is left alone entirely (committing there is what
arms a recording), which is why the toggle hides on that side.

It shipped in Settings → Capture first, and that was the wrong home: a
preference nobody finds is a preference nobody has, and this one went
unnoticed until it was pointed out. The row is where the decision is
already being made, so the control belongs there — with the hint pill
carrying its explanation on hover, since Qt tooltips are a coin toss on an
always-on-top frameless window.

Turning it on does not pre-select immediately. Pre-selecting stands the
chooser down, so the row would vanish from under the pointer that just
clicked it; the preference takes effect on the next overlay, which is the
only place "opens on" can mean anything. And because the chooser is down
whenever a region *is* pre-selected, the way back to the toggle is Esc —
which clears the selection and reopens the row, already this design's
documented route back to the mode.

This started life as a fifth mode row, and that was wrong. Picking a mode
from a dropdown every single time costs the same interaction the drag did,
so the mode spent exactly what it existed to save. A preference that is
simply on says "repeat captures are what I do" once, and then costs
nothing per snip.

The rectangle is remembered when a snip *completes*, not when a selection
is committed — a region dragged, reconsidered and cancelled is not what
anyone means by "the last one". It is stored absolute in `config.json`, so
it survives the restart autostart makes routine, and clipped to the
monitors that exist at recall time: a rectangle remembered on a
three-monitor desk and recalled on the laptop alone is trimmed to the
frame, and discarded outright if it no longer touches any real display or
survives only in a gap between them. A lasso is never stored, because its
bounding box is not what was captured.

**What happens to it** — `tokens.AFTER_CAPTURE`, surfaced per-snip: finish
instantly, annotate in place, save, or open the review window.

Two things diverge from the handoff here.

`save` is now a destination stills can pick, where the vocabulary used to
run instant/edit/review with `save` reserved for recordings. The split
action forced it: its caret has always offered Copy/Save/Open and
`_sync_bar_destination` has always mapped `save` to a Save face, so with no
stills id to record it in, Save was the one choice that could not be
remembered. It differs from `edit` in exactly one respect — which button is
primary — which is the whole of what "the chooser sets the split button's
face" means.

And the choice **is** remembered now. The handoff made this a per-snip
override that never wrote back, Settings holding the real preference. In
practice that read as the control being ignored: pick Copy, take the snip,
and the next one is back on Open. Last-used-wins is the rule instead — the
chooser's pill and the caret both persist, each side keeping its own
stored key, and the Settings radio is a starting point rather than a fixed
default.

## What it does not change

The bar's own capture-mode chip stays. Changing your mind mid-snip is a real
thing to want, and the chip is already the place that happens — the chooser
seeds it, and picking a mode either way keeps the two in step.

## Where it stands down

- **Once anything is selected.** It answers "what am I capturing", which
  stops being a question the moment something is. The floating bar has the
  opposite condition, so the two never share the screen.
- **Once Window or Freeform is armed.** Both need the whole screen — one
  previews whatever is under the cursor, the other is traced anywhere — and
  a bar across the top would be a band those modes cannot reach.
