"""One Hide sensitive read: the record of a read that is out, the worker
that does it off the UI thread, and which rectangles its answer says to
cover.

`OverlayWindow` owns the state machine -- when a read starts, what waits
on it, what the user is told -- because every step of that touches the
window: the selection, the toast, what Copy or Save was about to do. What
lives here is the part that does not: the `_SensitiveCheck` a read is
tracked by, the relay and worker that carry recognition to another thread
and its answer back, and `rects_to_hide`, which turns recognised lines
into the boxes to add.

Separate from `sensitive.py`, which finds sensitive values in text and
knows nothing of threads, windows or where a selection is. Nothing in
this module imports `overlay.py`.

Coordinates: recognition runs on a crop of the frozen frame and answers
in that crop's image pixels; `rects_to_hide` returns window-local logical
rects, the space marks live in.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from PyQt6.QtCore import QObject, QRectF, pyqtSignal
from PyQt6.QtGui import QImage

from snipux import log, sensitive


@dataclass(eq=False)
class _SensitiveCheck:
    """One Hide sensitive read that is still out with the recogniser.

    Compared by identity: it is also the token its own result comes back
    with, which is how a late answer is told from the current one.
    """

    # The selection that was read, in window coordinates, and that crop's
    # image pixels per logical unit on each axis -- what turns a word's
    # rect in the recognised image back into a place on this window.
    selection: QRectF
    scale_x: float
    scale_y: float
    custom: "sensitive.CustomList"
    # What Copy, Save, Open, Pin or an instant snip asked for while this
    # was out. Run once the boxes are on, never before.
    then: "Callable[[], None] | None" = None
    # The selection this read was for is gone or was replaced: its answer
    # is thrown away...
    discarded: bool = False
    # ...and, when another selection was committed meanwhile, that one is
    # read as soon as this comes back. One recognition at a time.
    recheck: bool = False


class _SensitiveCheckRelay(QObject):
    """Carries a recognition result from its worker thread to the UI thread.

    Its own object with no parent, not a signal on the overlay: a worker
    can outlive the window it read for, and emitting through a window Qt
    has already deleted raises in the worker. This one lives as long as
    anything still refers to it, and a connection to a deleted overlay is
    simply gone.
    """

    finished = pyqtSignal(object, object, object)  # check, lines | None, reason | None


def _recognize_for_check(recognize, image: QImage, check: _SensitiveCheck, relay) -> None:
    """The worker thread's whole job: read `image`, and say what came of it.

    `image` is this thread's own copy and nothing here touches a widget;
    the result goes back as a queued signal.
    """
    try:
        lines, reason = recognize(image), None
    except sensitive.RecognitionFailed as failure:
        lines, reason = None, str(failure)
    except Exception as exc:  # noqa: BLE001 -- the thread's outer boundary
        # Anything left uncaught would end the thread with no answer, and
        # the overlay would wait for one for ever with the snip held.
        log.note(f"Note: text recognition stopped unexpectedly: {exc!r}")
        lines, reason = None, "text recognition stopped unexpectedly"
    relay.finished.emit(check, lines, reason)


# Logical pixels added around each hidden value. OCR's word boxes sit
# tight on the glyphs, and the anti-aliased edges, descenders and
# accents that fall just outside them are exactly the slivers that
# make a covered value guessable.
HIDE_PADDING = 2.0

# A found value already this much covered by an existing solid box is
# not boxed again -- selecting a second region over the same text must
# not stack a duplicate of every box, each needing its own erase.
HIDE_ALREADY_COVERED = 0.9

# Two found boxes overlapping this much (of the smaller) are one value
# found twice -- recognition reads small selections at two sizes, and
# both reads usually find the same words -- and become one box.
HIDE_SAME_VALUE = 0.5


def rects_to_hide(check: _SensitiveCheck, lines: list, existing: list[QRectF]) -> list[QRectF]:
    """Where a solid box should go for each value `sensitive.find_sensitive`
    flags in `lines` -- including whatever the user added to their own
    hide list -- as window-local logical rects. `existing` is the solid
    boxes already on the snip, in that same space; a value one of them
    covers gets no second box.

    Coordinates: recognition happened on `Frame.crop`'s image, so every
    rect comes back in *cropped-image pixels*. Marks live in window
    coordinates. The mapping is the exact inverse of the one
    `shapes.render_selection` applies at export -- divide by that crop's
    own per-axis image-pixels-per-logical-unit ratio, then add the
    selection's origin -- which is what keeps a box over its word on a
    scaled display, where the two spaces differ by that ratio.
    """
    selection, scale_x, scale_y = check.selection, check.scale_x, check.scale_y
    existing = list(existing)
    pad = HIDE_PADDING
    found = [
        QRectF(
            selection.x() + image_rect.x() / scale_x - pad,
            selection.y() + image_rect.y() / scale_y - pad,
            image_rect.width() / scale_x + 2 * pad,
            image_rect.height() / scale_y + 2 * pad,
        )
        for image_rect in (
            finding.image_rect for finding in sensitive.find_sensitive(lines, check.custom)
        )
    ]
    rects = []
    for window_rect in _merge_same_values(found):
        if _already_covered(window_rect, existing):
            continue
        existing.append(window_rect)
        rects.append(window_rect)
    return rects


def _merge_same_values(rects: list[QRectF]) -> list[QRectF]:
    """`rects` with every pair that is the same value found twice
    replaced by their union. The union, not either one: the two reads
    rarely agree to the pixel, and covering both is what guarantees
    neither leaves an edge of the value showing."""
    merged: list[QRectF] = []
    for rect in rects:
        rect = QRectF(rect)
        joined = True
        while joined:
            joined = False
            for other in merged:
                overlap = rect.intersected(other)
                smaller = min(rect.width() * rect.height(), other.width() * other.height())
                if smaller > 0 and overlap.width() * overlap.height() >= smaller * HIDE_SAME_VALUE:
                    merged.remove(other)
                    rect = rect.united(other)
                    joined = True
                    break
        merged.append(rect)
    return merged


def _already_covered(rect: QRectF, covers: list[QRectF]) -> bool:
    area = rect.width() * rect.height()
    if area <= 0:
        return True
    for cover in covers:
        overlap = rect.intersected(cover)
        if overlap.width() * overlap.height() >= area * HIDE_ALREADY_COVERED:
            return True
    return False
