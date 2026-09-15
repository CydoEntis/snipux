"""Snap a highlighter sweep to the lines of text under it.

A hand-drawn highlight wanders: it starts a letter late, rides up over the
line above and stops short of the last word. This finds the lines of text a
sweep crossed and gives back one clean band per line, fitted to the words,
so the highlight reads the way a text editor's selection does.

It reads pixels, not words. The image is one already held in memory -- the
frozen frame on the overlay, the snip in the review window -- so this is
the same on every platform, needs no text recognition, and asks the OS for
nothing (CLAUDE.md's one rule). What it recognises is *ink on a flat
background*: a pixel far enough from the most common grey under the sweep.
Over a photo or a gradient there is no flat background, and it returns
nothing, which leaves the sweep as drawn.

Two gestures cross several lines, and both are read:

- **One swipe per line**, back and forth. Each line gets the words its own
  swipe covered.
- **One diagonal drag** from a word on one line to a word on a later one,
  read the way an editor reads a drag selection: the top line from the word
  the drag is at to the end of the line, every line between whole, the
  bottom line from its start to the word the drag is at. It is told apart
  from a swipe per line by how it covers the lines: a drag enters them in
  order, top down or bottom up, and each line's stretch carries on from the
  last one's instead of doubling back over it.

**Coordinates.** Everything here is in the pixel space of the image handed
in -- the stroke, its width, and the bands that come back. Converting a mark
into that space and back out is the caller's job (`marks.snap_to_text`).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from PyQt6.QtCore import QPointF, QRect, QRectF
from PyQt6.QtGui import QImage

# Grey levels between a pixel and the background for it to count as ink.
# Low enough for the anti-aliased body of small text and for grey-on-grey
# code comments; high enough that a panel a shade off the background, or
# JPEG noise, is not a line of text.
INK_CONTRAST = 56

# The share of the pixels under a sweep the background must hold -- within a
# few grey levels -- for there to be a background at all. A photo spreads
# its pixels over many greys and never reaches it.
FLAT_BACKGROUND_SHARE = 0.40
BACKGROUND_TOLERANCE = 6

# A gap between two inked columns up to this many line heights is inside a
# word; wider is the space between words. Proportional fonts put a word
# space near a quarter of the line height, letters well under a tenth.
WORD_GAP = 0.18
# Up to this many line heights is still the same line of text. Wider is a
# column gutter, or the edge of the text.
LINE_GAP = 1.2

# How far, in line heights, one line's stretch of a drag selection may reach
# back over the line before's and still carry on from it.
DRAG_OVERLAP = 1.5

# A band reaches this far above the line's x-height and below its baseline,
# in x-heights, so that lines with and without ascenders and descenders get
# the same band -- the height of the type, not of the letters that happen to
# be in it.
ASCENT = 0.6
DESCENT = 0.45
# Rows holding this share of the busiest row's ink are the x-height.
X_HEIGHT_SHARE = 0.4

# Rows are lines where they hold ink, with nothing bridged by the stroke's
# width: a terminal leaves two or three blank rows between lines, and on a
# scaled display a stroke-sized bridge crossed them and made a paragraph one
# line. Only a fragment -- the dots over a row of i's, an accent -- joins the
# line beside it: one no taller than this share of that line, and no further
# from it than `FRAGMENT_REACH` of its height.
FRAGMENT_SHARE = 0.4
FRAGMENT_REACH = 0.35
# Lines with no blank row between them at all -- a descender touching the
# ascender below -- are told apart by their x-heights: runs of rows holding
# this share of the busiest row's ink, centres at least `LINE_PITCH` of the
# taller one's height apart. Closer than that is one line whose middle rows
# happen to be light, as the middle of an o is.
CORE_SHARE = 0.35
LINE_PITCH = 1.6
# A line the sweep only brushed -- fewer than this share of the points of the
# line it spent longest on -- is the hand wandering, not a line meant.
BRUSHED_SHARE = 0.25


@dataclass
class _Line:
    """One run of inked rows the sweep reached, in the region's rows."""

    top: int
    bottom: int
    columns: bytes  # 1 where any row of the line has ink, across the image
    reach: list[float]  # the x of every sweep point that fell on this line, in order
    first_point: int  # index of the first sweep point on this line

    @property
    def height(self) -> int:
        return self.bottom - self.top + 1


def text_bands(image: QImage, stroke: list[QPointF], stroke_width: float) -> list[QRectF]:
    """One band per line of text `stroke` swept, fitted to the words it
    covered, top to bottom -- or no bands when it swept no text.

    `stroke` is the sweep's points and `stroke_width` the width it was
    painted at, both in `image`'s pixels.
    """
    if image.isNull() or len(stroke) < 2 or stroke_width <= 0:
        return []
    points = _densified(stroke, max(1.0, stroke_width / 4))

    # The rows the sweep could have meant: its own reach, a pen width more
    # either way. Every column, because a line crossed in passing is taken
    # to its ends, and they can be well outside the sweep.
    region_top = max(0, math.floor(min(p.y() for p in points) - stroke_width))
    region_bottom = min(image.height() - 1, math.ceil(max(p.y() for p in points) + stroke_width))
    if region_bottom < region_top:
        return []
    rows = _grey_rows(image, QRect(0, region_top, image.width(), region_bottom - region_top + 1))
    width = image.width()

    half = stroke_width / 2
    left = max(0, math.floor(min(p.x() for p in points) - half))
    right = min(width - 1, math.ceil(max(p.x() for p in points) + half))
    if right < left:
        return []
    background = _flat_background(b"".join(row[left : right + 1] for row in rows))
    if background is None:
        return []
    is_ink = bytes(1 if abs(grey - background) >= INK_CONTRAST else 0 for grey in range(256))
    masks = [row.translate(is_ink) for row in rows]

    lines = _lines_swept(masks, left, right, points, region_top, stroke_width)
    if not lines:
        return []

    by_height = sorted(lines, key=lambda line: line.top)
    downward = _drag_direction(lines)

    bands = []
    last = len(by_height) - 1
    for index, line in enumerate(by_height):
        if downward is None:
            span = _swiped_span(line)
        else:
            span = _dragged_span(line, index == 0, index == last, downward)
        if span is None:
            continue
        band = _band(masks, line, *span)
        bands.append(band.translated(0, region_top))
    return bands


def _densified(points: list[QPointF], step: float) -> list[QPointF]:
    """`points` with more in between wherever two are further apart than
    `step`, so a fast sweep that skipped a line between two mouse moves still
    has a point on it.
    """
    out = [points[0]]
    for start, end in zip(points, points[1:]):
        distance = math.hypot(end.x() - start.x(), end.y() - start.y())
        steps = max(1, math.ceil(distance / step))
        for i in range(1, steps + 1):
            t = i / steps
            out.append(QPointF(start.x() + (end.x() - start.x()) * t,
                               start.y() + (end.y() - start.y()) * t))
    return out


def _grey_rows(image: QImage, rect: QRect) -> list[bytes]:
    """`rect` of `image` as one bytes of greys per row, `rect`'s own width.

    Through `bytes` rather than a pixel at a time: `translate`, `count` and
    `find` then do the per-pixel work in C, which is what keeps a 4K frame
    under a moment on release with no numerical library.
    """
    grey = image.copy(rect).convertToFormat(QImage.Format.Format_Grayscale8)
    stride = grey.bytesPerLine()
    bits = grey.constBits()
    bits.setsize(grey.sizeInBytes())
    data = bytes(bits)
    width = grey.width()
    return [data[row * stride : row * stride + width] for row in range(grey.height())]


def _flat_background(sample: bytes) -> int | None:
    """The grey most of `sample` is, or None when no one grey is most of it."""
    if not sample:
        return None
    counts = [sample.count(grey) for grey in range(256)]
    mode = max(range(256), key=counts.__getitem__)
    near = sum(counts[max(0, mode - BACKGROUND_TOLERANCE) : mode + BACKGROUND_TOLERANCE + 1])
    return mode if near >= FLAT_BACKGROUND_SHARE * len(sample) else None


def _lines_swept(
    masks: list[bytes],
    left: int,
    right: int,
    points: list[QPointF],
    region_top: int,
    stroke_width: float,
) -> list[_Line]:
    """The lines of text in `masks` that the sweep's points fall on.

    Rows are lines by the ink between `left` and `right`, the sweep's own
    columns: `_text_rows` finds them. A run too short to be type, or too tall
    to be one line of it, is not a line. Each point goes to the nearest line
    within a quarter of a line height, so a sweep riding between two lines
    lands on one of them rather than both, and a line only brushed is dropped.
    """
    counts = [mask[left : right + 1].count(1) for mask in masks]
    tallest = max(stroke_width * 4, 64.0)
    runs = [(top, bottom) for top, bottom in _text_rows(counts) if 3 <= bottom - top + 1 <= tallest]
    if not runs:
        return []

    reach: dict[int, list[float]] = {}
    first: dict[int, int] = {}
    for index, point in enumerate(points):
        y = point.y() - region_top
        best, best_distance = None, None
        for run_index, (top, bottom) in enumerate(runs):
            distance = max(top - y, y - bottom, 0.0)
            if distance <= (bottom - top + 1) * 0.25 and (
                best_distance is None or distance < best_distance
            ):
                best, best_distance = run_index, distance
        if best is not None:
            reach.setdefault(best, []).append(point.x())
            first.setdefault(best, index)
    if not reach:
        return []

    longest = max(len(xs) for xs in reach.values())
    lines = []
    for run_index, xs in reach.items():
        if len(xs) < max(2, BRUSHED_SHARE * longest):
            continue
        top, bottom = runs[run_index]
        lines.append(_Line(top, bottom, _columns(masks[top : bottom + 1]), xs, first[run_index]))
    return lines


def _text_rows(counts: list[int]) -> list[tuple[int, int]]:
    """Runs of rows that are one line of text each, from each row's ink.

    Inked rows first, with no gap bridged; then fragments joined to the line
    beside them; then any run holding several touching lines split between
    their x-heights.
    """
    runs = []
    start = None
    for row, count in enumerate(counts):
        if count and start is None:
            start = row
        elif not count and start is not None:
            runs.append((start, row - 1))
            start = None
    if start is not None:
        runs.append((start, len(counts) - 1))

    joined = True
    while joined:
        joined = False
        for index, (top, bottom) in enumerate(runs):
            height = bottom - top + 1
            nearest = None
            for other in (index - 1, index + 1):
                if not 0 <= other < len(runs):
                    continue
                other_top, other_bottom = runs[other]
                other_height = other_bottom - other_top + 1
                gap = other_top - bottom - 1 if other > index else top - other_bottom - 1
                if height <= FRAGMENT_SHARE * other_height and gap <= max(
                    2, round(FRAGMENT_REACH * other_height)
                ):
                    if nearest is None or gap < nearest[1]:
                        nearest = (other, gap)
            if nearest is not None:
                low, high = sorted((index, nearest[0]))
                runs[low : high + 1] = [(runs[low][0], runs[high][1])]
                joined = True
                break

    lines = []
    for top, bottom in runs:
        lines.extend(_split_touching(counts, top, bottom))
    return lines


def _split_touching(counts: list[int], top: int, bottom: int) -> list[tuple[int, int]]:
    """`top`..`bottom` cut into lines between x-heights, or left whole."""
    rows = counts[top : bottom + 1]
    peak = max(rows)
    cores: list[list[int]] = []
    for row, count in enumerate(rows):
        if count < peak * CORE_SHARE:
            continue
        if cores and row - cores[-1][1] <= 2:
            cores[-1][1] = row
        else:
            cores.append([row, row])
    if len(cores) < 2:
        return [(top, bottom)]

    pieces = []
    start = 0
    for (above_top, above_bottom), (below_top, below_bottom) in zip(cores, cores[1:]):
        pitch = (below_top + below_bottom) / 2 - (above_top + above_bottom) / 2
        taller = max(above_bottom - above_top, below_bottom - below_top) + 1
        if pitch < LINE_PITCH * taller or below_top - above_bottom < 2:
            continue
        middle = (above_bottom + below_top) / 2
        cut = min(range(above_bottom + 1, below_top), key=lambda r: (rows[r], abs(r - middle)))
        pieces.append((top + start, top + cut))
        start = cut + 1
    pieces.append((top + start, bottom))
    return pieces


def _columns(masks: list[bytes]) -> bytes:
    """1 for every column any of `masks` has ink in.

    Each mask is all 0 and 1 bytes, so or-ing them as big integers ors every
    column at once and cannot carry into the next.
    """
    combined = 0
    for mask in masks:
        combined |= int.from_bytes(mask, "big")
    return combined.to_bytes(len(masks[0]), "big")


def _drag_direction(lines: list[_Line]) -> bool | None:
    """True for a drag selection made top down, False for one made bottom
    up, None for anything else -- one line, or a swipe per line.

    A drag enters its lines in order of height, and its stretch on each line
    carries on from the one before: all left to right, or all right to left,
    never doubling back over more than `DRAG_OVERLAP` line heights of it. A
    swipe per line goes back over the same columns on every line.
    """
    if len(lines) < 2:
        return None
    by_order = sorted(lines, key=lambda line: line.first_point)
    by_height = sorted(lines, key=lambda line: line.top)
    if by_order == by_height:
        downward = True
    elif by_order == by_height[::-1]:
        downward = False
    else:
        return None
    steps = list(zip(by_order, by_order[1:]))
    for rightward in (True, False):
        if all(
            _carries_on(before, after, rightward) for before, after in steps
        ):
            return downward
    return None


def _carries_on(before: _Line, after: _Line, rightward: bool) -> bool:
    slack = DRAG_OVERLAP * max(before.height, after.height)
    if rightward:
        return min(after.reach) >= max(before.reach) - slack
    return max(after.reach) <= min(before.reach) + slack


def _swiped_span(line: _Line) -> tuple[int, int] | None:
    """The words a swipe along `line` covered, or None if it covered no ink."""
    columns = line.columns
    word_gap = _word_gap(line)
    reach_left = max(0, math.floor(min(line.reach)))
    reach_right = min(len(columns) - 1, math.ceil(max(line.reach)))
    start = columns.find(1, reach_left, reach_right + 1)
    if start == -1:
        return None
    end = columns.rfind(1, reach_left, reach_right + 1)
    return _run_start(columns, start, word_gap), _run_end(columns, end, word_gap)


def _dragged_span(
    line: _Line, top: bool, bottom: bool, downward: bool
) -> tuple[int, int] | None:
    """`line`'s part of a drag selection, or None if there is no ink on it.

    Where the drag is on the top and bottom lines is where it began or ended
    there: the top line's first point on a drag made top down, its last on
    one made bottom up, and the other way round for the bottom line.
    """
    columns = line.columns
    word_gap = _word_gap(line)
    line_gap = _line_gap(line)
    if top:
        at = line.reach[0] if downward else line.reach[-1]
        start = _word_from(columns, round(at), word_gap)
        if start is None:
            return None
        return start, _run_end(columns, start, line_gap)
    if bottom:
        at = line.reach[-1] if downward else line.reach[0]
        end = _word_to(columns, round(at), word_gap)
        if end is None:
            return None
        return _run_start(columns, end, line_gap), end
    inside = columns.find(1, max(0, math.floor(min(line.reach))),
                          min(len(columns), math.ceil(max(line.reach)) + 1))
    if inside == -1:
        return None
    return _run_start(columns, inside, line_gap), _run_end(columns, inside, line_gap)


def _word_gap(line: _Line) -> int:
    return max(1, round(line.height * WORD_GAP))


def _line_gap(line: _Line) -> int:
    return max(_word_gap(line) + 1, round(line.height * LINE_GAP))


def _word_from(columns: bytes, at: int, gap: int) -> int | None:
    """The start of the word at column `at`, or of the next one when `at` is
    in the space before it. None when nothing follows on the line.
    """
    at = min(max(at, 0), len(columns) - 1)
    behind = columns.rfind(1, 0, at + 1)
    if behind != -1 and at - behind <= gap:
        return _run_start(columns, behind, gap)
    ahead = columns.find(1, at)
    return None if ahead == -1 else ahead


def _word_to(columns: bytes, at: int, gap: int) -> int | None:
    """The end of the word at column `at`, or of the one before when `at` is
    in the space after it. None when nothing comes before it on the line.
    """
    at = min(max(at, 0), len(columns) - 1)
    ahead = columns.find(1, at)
    if ahead != -1 and ahead - at <= gap:
        return _run_end(columns, ahead, gap)
    behind = columns.rfind(1, 0, at + 1)
    return None if behind == -1 else _run_end(columns, behind, gap)


def _run_start(columns: bytes, index: int, gap: int) -> int:
    """Where the ink at `index` starts, bridging gaps of up to `gap` columns."""
    while True:
        blank = columns.rfind(0, 0, index)
        start = blank + 1
        previous = columns.rfind(1, 0, start) if start > 0 else -1
        if previous == -1 or start - previous - 1 > gap:
            return start
        index = previous


def _run_end(columns: bytes, index: int, gap: int) -> int:
    """Where the ink at `index` ends, bridging gaps of up to `gap` columns."""
    while True:
        blank = columns.find(0, index)
        end = (len(columns) if blank == -1 else blank) - 1
        following = columns.find(1, end + 1)
        if following == -1 or following - end - 1 > gap:
            return end
        index = following


def _band(masks: list[bytes], line: _Line, start: int, end: int) -> QRectF:
    """The band over columns `start`..`end` of `line`, in the region's rows.

    Sized from the type rather than the letters: the x-height is found from
    the rows busiest with ink, and the band reaches `ASCENT` above it and
    `DESCENT` below, and never less than the ink itself.
    """
    counts = [mask[start : end + 1].count(1) for mask in masks[line.top : line.bottom + 1]]
    inked = [row for row, count in enumerate(counts) if count]
    ink_top, ink_bottom = inked[0], inked[-1]
    busiest = max(counts)
    core = [row for row, count in enumerate(counts) if count >= busiest * X_HEIGHT_SHARE]
    x_height = core[-1] - core[0] + 1
    top = min(ink_top, core[0] - ASCENT * x_height)
    bottom = max(ink_bottom + 1, core[-1] + 1 + DESCENT * x_height)
    pad = max(1.0, round((bottom - top) * 0.08))
    return QRectF(
        start - pad,
        line.top + top - pad,
        end - start + 1 + 2 * pad,
        bottom - top + 2 * pad,
    )
