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

**What happens to it** — the same three behaviours `tokens.AFTER_CAPTURE`
describes, surfaced per-snip: open the review window, copy and get out of
the way, or save silently. It is a per-snip answer because it changes far
more often than a setting should have to; leaving it alone keeps whatever
Settings says, and `OverlayWindow.outcome` is `None` in that case.

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
