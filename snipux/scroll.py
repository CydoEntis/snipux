"""Drive a page past itself, one screenful at a time (SNX-2).

The other half of full-page capture. `stitch.py` puts the frames back
together; this decides what frames to take.

It is the one place in snipux that breaks the rule the rest of it is built
on. CLAUDE.md: capture the whole virtual desktop **in a single shot**, "the
compositor/OS is involved for exactly one instant." A page taller than the
screen cannot be had that way -- the pixels do not exist anywhere until
something scrolls to them. The project reserved this exception explicitly
("the capture layer should not assume one frame per session"), and keeping
it in a module of its own is what stops it leaking into the modes that do
obey the rule.

Everything here is measured rather than assumed, because on a real browser
almost none of the obvious approaches work:

* **`PostMessage(WM_MOUSEWHEEL)` does not scroll Chromium at all.**
  Measured: 1311 of 1311 rows unchanged. It ignores posted wheel messages.
* **`SendInput` works only once the window has focus.** So a full-page
  capture cannot quietly scroll a background window; it takes focus, and
  has to give it back.
* **How far a notch scrolls is unknowable in advance** -- it depends on the
  user's mouse settings, the page's own smooth-scrolling, and page zoom. So
  every distance here is *measured from the pixels afterwards*, never
  predicted, and `stitch` is what measures it.

The driver is injected (`grab`, `scroll`, `settle`) so the loop can be
tested without a browser, a desktop, or a real mouse.
"""

from __future__ import annotations

from PyQt6.QtGui import QImage

from snipux.stitch import StitchError, row_signatures, stitch

# How many screenfuls to allow before giving up. A page that keeps growing
# as it is scrolled -- infinite feeds, "load more" on scroll -- has no
# bottom to reach, and scrolling one forever is worse than stopping and
# saying so. Twenty screens is far more than anything anyone wants as a
# single image and is reached in seconds.
MAX_FRAMES = 20

# Grabs whose rows are unchanged from the one before mean the page did not
# move: the bottom. Two in a row are required, because one can also mean the
# scroll had not finished when the grab was taken.
STILL_GRABS_TO_STOP = 2


class ScrollCaptureError(RuntimeError):
    """Raised when a page cannot be captured whole -- see `capture_page`."""


def capture_page(
    grab,
    scroll,
    settle,
    max_frames: int = MAX_FRAMES,
) -> QImage:
    """Scroll from wherever the page is now to its bottom, and return the
    whole thing as one image.

    `grab()` returns the viewport as a `QImage`. `scroll()` moves the page
    down by less than a viewport -- and by enough less to leave more than
    `stitch.PROBE_ROWS` of overlap, because the join is found by matching a
    run of that many rows. Two frames can genuinely overlap and still be
    unjoinable if the shared band is thinner than the probe.

    `settle()` waits for the scroll to finish painting.

    The caller is responsible for scrolling to the top first and for
    restoring focus afterwards; this function starts where it is put.

    Stops when the page stops moving, which is the bottom -- detected, not
    predicted, because the number of scrolls a page needs cannot be known
    before scrolling it.

    Raises `ScrollCaptureError` rather than returning a partial image. A
    full-page capture missing a band in the middle is worse than one that
    failed: the user cannot see what is absent from a picture of a page
    they were scrolling past.
    """
    first = grab()
    if first is None or first.isNull():
        raise ScrollCaptureError("nothing to capture")

    frames = [first]
    signatures = [row_signatures(first)]
    still = 0

    while len(frames) < max_frames:
        scroll()
        settle()
        current = grab()
        if current is None or current.isNull():
            raise ScrollCaptureError("a grab came back empty mid-scroll")

        current_signature = row_signatures(current)
        if current_signature == signatures[-1]:
            still += 1
            if still >= STILL_GRABS_TO_STOP:
                break
            # One unchanged grab can also mean the scroll had not landed
            # yet, so try again before calling it the bottom.
            continue
        still = 0
        frames.append(current)
        signatures.append(current_signature)

    if len(frames) >= max_frames:
        raise ScrollCaptureError(
            f"the page was still growing after {max_frames} screens -- an "
            "endless feed cannot be captured as one image"
        )

    try:
        return stitch(frames)
    except StitchError as exc:
        # Re-raised in this module's own vocabulary so a caller handles one
        # kind of failure, not two, and the cause survives.
        raise ScrollCaptureError(str(exc)) from exc
