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

NOT REACHABLE FROM THE UI TODAY, AND THAT IS DELIBERATE. Full-page capture
was built, shipped in 0.4.x, and taken back off the chooser in 0.5.0. This
module and `stitch.py` are correct as far as they go -- driven against a
real browser they scrolled a real page and joined six frames into one
2811px image with no seam -- and they are kept, with their tests, because
the missing piece is one function's worth of work rather than a rewrite.

What is missing: `stitch.find_overlap` requires runs of **pixel-identical**
rows to locate a join. That holds on a static page and fails on anything
that redraws itself between grabs. Measured on a real site with live
charts, every scroll from 1 to 9 notches reported no overlap at all -- and
measured on a page with playing video, only 3.5% of pixels actually
changed. The information is there; the matcher is too strict to see it.
Tolerating a small fraction of unmatched rows is what would make this
shippable, and until it exists the mode works on some of the web and fails
on the rest, which is worse than not offering it.

See SNX-2 and docs/design/browser-capture.md.
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


# How many settle waits to allow for a page to stop moving before the first
# frame is taken. A jump to the top of a long page is a smooth-scroll
# animation of thousands of pixels and takes far longer than one settle.
MAX_SETTLE_WAITS = 12


class ScrollCaptureError(RuntimeError):
    """Raised when a page cannot be captured whole -- see `capture_page`."""


def wait_until_still(grab, settle, max_waits: int = MAX_SETTLE_WAITS):
    """Wait for the page to stop moving, and return the settled frame.

    Needed because a capture does not begin where the page happens to be
    left: it jumps to the top first, and on a long page that is a
    smooth-scroll animation of thousands of pixels. One fixed settle is not
    remotely enough, and grabbing mid-flight yields a first frame that
    overlaps nothing -- reported as "frames 0 and 1 do not overlap", which
    is true and was the capture's own fault rather than the page's.

    Two identical grabs in a row is the signal, the same one `capture_page`
    uses for the bottom: an animation cannot produce two matching frames
    while it is running.

    Returns the last frame taken even if it never settles, rather than
    raising -- a page that never stops changing at all is a real thing (a
    playing video, a ticker), and it is `find_overlap`'s job to say whether
    what it produced can be joined, not this function's to refuse first.
    """
    previous = grab()
    previous_signature = row_signatures(previous)
    for _ in range(max_waits):
        settle()
        current = grab()
        current_signature = row_signatures(current)
        if current_signature == previous_signature:
            return current
        previous, previous_signature = current, current_signature
    return previous


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
    # Settled first, not simply grabbed: the caller has usually just sent
    # the page to the top, and that is an animation.
    first = wait_until_still(grab, settle)
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
