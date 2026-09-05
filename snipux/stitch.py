"""Assemble a tall image from overlapping screenshots of a scrolling page.

The half of full-page capture that decides whether the feature is worth
having. Scrolling a browser is easy; putting the frames back together
without a seam, a repeated header, or a lost strip of content is not, and
this module is deliberately built and judged **before** anything is taught
to scroll (docs/design/browser-capture.md).

Three problems, in order of how badly each shows:

**The scroll amount is a lie.** Smooth scrolling, per-user scroll settings,
page zoom and a page that grows as it loads all mean the number of pixels
asked for is not the number that moved. Every offset here is *measured*
from the pixels, never assumed from the request.

**Sticky headers repeat.** A header that stays put appears at the top of
every frame, so a naive join stacks it once per scroll. The signal is that
those rows never change; the constraint that keeps it safe is that they
must be anchored to an edge (see `sticky_bands`).

**No numpy, no OpenCV.** CLAUDE.md bans both, and a screenshot tool that
drags in a numerical stack has made a bad trade. So this compares *row
signatures* -- one cheap hash per scanline, computed once per frame --
rather than pixels. A 2560x1311 frame becomes 1311 integers, and finding
the overlap between two frames is then integer comparison over a few
thousand candidates instead of billions of pixel reads.
"""

from __future__ import annotations

import zlib

from PyQt6.QtGui import QImage, QPainter

# Rows of `later` matched against `earlier` to locate the join. Enough that
# a run of identical rows -- a blank gap between paragraphs, a flat band of
# page background -- cannot match by accident, since a false match here
# silently duplicates or deletes content. Small enough to stay cheap.
PROBE_ROWS = 24

# A scroll must move at least this many rows to count. Below it the two
# frames are the same picture, which is how the bottom of a page is
# recognised: the page stopped moving.
MIN_SCROLL_ROWS = 8


# Pixels ignored at the right edge when hashing a row. The scrollbar lives
# there, it is present in *every* row, and it moves as the page scrolls --
# so including it makes almost every row differ from its own self one scroll
# later. Measured on a real page: a single scroll left 482 of 1311 rows
# matchable with the scrollbar included, and 1008 with it excluded, which is
# every row that had not been scrolled off. Generous enough for a scrollbar
# at 150% scaling; it costs only matching accuracy at the extreme right
# edge, never a pixel of the stitched image.
SCROLLBAR_MARGIN = 24


def row_signatures(image: QImage, ignore_right: int = SCROLLBAR_MARGIN) -> list[int]:
    """One hash per scanline, top to bottom.

    The whole reason this module is fast enough to exist without numpy.
    Hashing each row once turns "do these two frames overlap, and by how
    much" into integer comparison instead of a pixel-by-pixel search.

    `crc32` rather than a cryptographic hash: this is looking for rows that
    are *identical*, not defending against anyone constructing a collision,
    and it is implemented in C in the standard library.

    The whole scanline is hashed, not a sample of it. Sampling every eighth
    pixel would be faster and would also make two rows that differ only in
    the columns it skipped -- a thin vertical rule, a cursor, a scrollbar
    -- indistinguishable, which is exactly the mistake that produces a join
    in the wrong place.
    """
    if image.isNull():
        return []
    # Normalised so two frames captured through different paths (a crop of
    # the frozen frame, a direct grab) cannot differ by format alone and
    # hash differently for pixels that are the same colour.
    if image.format() != QImage.Format.Format_RGB32:
        image = image.convertToFormat(QImage.Format.Format_RGB32)

    width, height = image.width(), image.height()
    # RGB32: four bytes a pixel. The right margin is dropped before hashing
    # -- see SCROLLBAR_MARGIN -- but never below a quarter of the frame, so
    # a narrow image cannot end up hashing almost nothing and matching
    # everything.
    keep = max(width - max(ignore_right, 0), width // 4, 1)
    usable = keep * 4
    signatures = []
    for y in range(height):
        line = image.constScanLine(y)
        line.setsize(image.bytesPerLine())
        signatures.append(zlib.crc32(bytes(line)[:usable]))
    return signatures


def find_overlap(
    earlier: list[int],
    later: list[int],
    probe_rows: int = PROBE_ROWS,
    min_scroll: int = MIN_SCROLL_ROWS,
) -> int | None:
    """How many rows at the top of `later` repeat the bottom of `earlier`.

    Takes signature lists rather than images so a caller hashing a run of
    frames pays for each one once.

    Returns the overlap in rows, or None when no overlap can be *measured*.
    None covers three different situations and deliberately does not try to
    tell them apart, because the caller's answer to all three is the same:
    stop, do not guess.

    * The page did not move -- the bottom of the document. This is how the
      end is detected rather than predicted.
    * It moved further than one screenful, so the frames share no rows.
    * It changed underneath the capture.

    **There must be real overlap.** Two frames that abut exactly share no
    rows, so there is nothing to match on and nothing to verify against;
    that returns None too. A scroll driver must therefore always scroll
    *less* than a viewport, which it wants to do anyway -- overlap is the
    only evidence that the frames belong together.

    Searched from the smallest scroll upwards, so the largest overlap that
    explains the frames wins. That matters on a page of repeating structure
    -- a list of near-identical rows -- where several offsets match: the
    shortest scroll is the one that actually happened, since a longer one
    would require the page to have moved further than it was asked to.
    Ambiguity remains when the repeat period is *shorter* than the real
    scroll, and no amount of pixel comparison can resolve that.

    A scroll shorter than `min_scroll` is not looked for at all, which is
    what stops a page that never moved from matching itself perfectly.
    """
    if not earlier or not later:
        return None
    probe = later[: min(probe_rows, len(later))]
    if not probe:
        return None

    highest = len(earlier) - len(probe)
    for start in range(max(min_scroll, 0), highest + 1):
        if earlier[start : start + len(probe)] == probe:
            return len(earlier) - start
    return None


def sticky_bands(frames: list[list[int]]) -> tuple[int, int]:
    """`(header rows, footer rows)` that never change across `frames`.

    A sticky header appears at the top of every frame, so joining frames
    without removing it stacks one copy per scroll.

    **Edge-anchored, and that is the load-bearing part.** "Rows that are
    identical in every frame" on its own also describes a band of flat page
    background in the middle of a document, and cropping *that* would
    delete real content. A header is pinned to the top and a footer to the
    bottom, so only unbroken runs reaching from an edge count -- the run
    stops at the first row that ever changes.

    Needs at least two frames; one frame has nothing to be constant
    against, and everything would look sticky.
    """
    if len(frames) < 2:
        return 0, 0
    height = min(len(f) for f in frames)
    if height == 0:
        return 0, 0
    first = frames[0]

    header = 0
    while header < height and all(f[header] == first[header] for f in frames):
        header += 1

    footer = 0
    while footer < height - header and all(
        f[len(f) - 1 - footer] == first[len(first) - 1 - footer] for f in frames
    ):
        footer += 1

    return header, footer


class StitchError(RuntimeError):
    """Raised when frames cannot be assembled -- see `stitch`."""


def stitch(images: list[QImage], drop_sticky: bool = True) -> QImage:
    """Join overlapping frames, top to bottom, into one tall image.

    `images` are in capture order, each overlapping the one before it.

    Raises `StitchError` rather than returning a plausible-looking wrong
    picture when a pair does not overlap. A full-page capture that silently
    drops or duplicates a band is worse than one that says it could not do
    it: the user cannot see what is missing from an image of a page they
    were scrolling past.

    `drop_sticky` removes an unchanging header from every frame after the
    first, and an unchanging footer from every frame before the last, so
    each survives exactly once -- at the top and bottom of the result,
    where the page actually puts them.
    """
    if not images:
        raise StitchError("nothing to stitch")
    if len(images) == 1:
        return images[0].copy()

    signatures = [row_signatures(image) for image in images]
    if any(not s for s in signatures):
        raise StitchError("an empty frame cannot be stitched")

    header, footer = sticky_bands(signatures) if drop_sticky else (0, 0)

    # Overlaps are measured between the *scrolling* parts of the frames.
    # A sticky band does not move, so leaving it in would let a run of
    # frozen rows match anywhere and put the join in the wrong place.
    def body(index: int) -> list[int]:
        end = len(signatures[index]) - footer
        return signatures[index][header:end]

    overlaps: list[int] = []
    for index in range(1, len(images)):
        overlap = find_overlap(body(index - 1), body(index))
        if overlap is None:
            raise StitchError(
                f"frames {index - 1} and {index} do not overlap -- the page "
                "moved further than one screen, or changed while it was "
                "being captured"
            )
        overlaps.append(overlap)

    width = min(image.width() for image in images)
    total = images[0].height()
    for index in range(1, len(images)):
        total += images[index].height() - header - footer - overlaps[index - 1]

    canvas = QImage(width, total, QImage.Format.Format_RGB32)
    canvas.fill(0)
    painter = QPainter(canvas)
    try:
        # The first frame whole -- header included, since that is where the
        # page puts it.
        painter.drawImage(0, 0, images[0], 0, 0, width, images[0].height() - footer)
        y = images[0].height() - footer
        for index in range(1, len(images)):
            image = images[index]
            skip = header + overlaps[index - 1]
            keep = image.height() - skip - footer
            if keep > 0:
                painter.drawImage(0, y, image, 0, skip, width, keep)
                y += keep
        # The last frame's footer, once, at the bottom.
        if footer > 0:
            last = images[-1]
            painter.drawImage(0, y, last, 0, last.height() - footer, width, footer)
    finally:
        # Never left open across a read of the image it painted, per
        # CLAUDE.md -- the caller reads `canvas` the moment this returns.
        painter.end()
    return canvas
