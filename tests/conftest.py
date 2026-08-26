"""Shared pytest fixtures and platform-skip helpers.

snipux's one architectural rule is Linux-only (capture.py grabs the virtual
desktop through X11/Wayland compositor APIs), but the suite itself runs on
whatever the developer is sitting at -- CLAUDE.md is explicit that day-to-day
development happens on Windows and in an Ubuntu VM. A red run that nobody can
trust is worse than no run at all, so a handful of tests that exercise
genuinely OS-specific behaviour (real window activation semantics, named-pipe
timing) are marked with `skip_on_windows` rather than fixed to "pass
everywhere" or silently deleted. That keeps the *reason* for the platform gap
in the suite's own output.

A test whose only platform dependency is *which fallback font this machine
happens to have installed* is a different case, and does not belong behind
`skip_on_windows` -- that is a fact about the box running the suite, not
about the OS family (an earlier version of
`TestTheDestinationMenuFitsItsWidth.test_every_note_fits_without_eliding` in
test_overlay.py assumed otherwise, and was wrong on at least one real Linux
machine). That test instead skips itself at runtime, naming the actual
fallback family it resolved, only once it has confirmed the assertion would
fail for the known cause -- IBM Plex not being vendored (design/fonts/
doesn't exist in this handoff) -- rather than assuming a whole OS one way or
the other.
"""

import sys

import pytest

# The suite runs under a single shared QApplication per process (each test
# module's own autouse fixture reuses whatever instance already exists), so
# window-activation state leaks across files the same way it would in a real
# session. Windows enforces real OS-level window activation even under the
# offscreen QPA platform; X11/Wayland's offscreen backend does not. A handful
# of hover/cursor tests depend on synthetic QTest.mouseMove events reaching a
# freshly-shown window as the active one, which only holds on the target
# platform.
ON_WINDOWS = sys.platform == "win32"


def skip_on_windows(reason: str):
    """Mark a test as expected to fail on Windows for a platform reason,
    naming that reason, rather than either forcing it green everywhere or
    deleting the coverage it represents on Linux.
    """
    return pytest.mark.skipif(ON_WINDOWS, reason=reason)
