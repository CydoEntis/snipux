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

import os
import sys

# CLAUDE.md requires this suite to pass headless -- a build machine has no
# display, and neither does a Hopper worktree -- so the QPA platform is
# defaulted here rather than left to every caller to prefix.
#
# It is here, and not in the command, because the prefix form is POSIX-only:
# `QT_QPA_PLATFORM=offscreen python -m pytest -q` under cmd.exe fails with
# "'QT_QPA_PLATFORM' is not recognized as an internal or external command",
# 13ms in, having run no tests at all. Moving the assignment into a
# `python -c` one-liner instead only traded that for the shell eating the
# quotes ("SyntaxError: unterminated string literal"). A command with no
# shell syntax in it cannot be got wrong by whichever shell runs it.
#
# `setdefault`, not assignment: an explicitly exported QT_QPA_PLATFORM still
# wins, so a developer can still run the suite against a real display, and
# the documented `QT_QPA_PLATFORM=offscreen python -m pytest -q` keeps
# meaning exactly what it says.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# The offscreen plugin has no windowing system to ask for fonts, so it builds
# its font database from a directory instead. On Linux it finds one through
# fontconfig and the suite measures real glyphs; on Windows there is no
# fontconfig, nothing tells it where to look, and `QFontDatabase.families()`
# comes back *empty*. That does not fail loudly -- QFont resolves to a null
# family, `drawText` paints nothing at all, and `QFontMetrics` reports one
# advance of exactly the pixel size for every character. Assertions about
# text fitting a box then measure a fiction: a 22-character note "advances"
# 242px, and two different countdown numerals grab as byte-identical images
# because neither of them drew a glyph.
#
# Pointing the plugin at the system font directory makes those measurements
# mean on Windows what they already mean on Linux. Guarded by platform
# because on Linux this would *narrow* a working fontconfig database to a
# single directory, which is a regression, not a fix.
#
# setdefault again, so a developer aiming the suite at a vendored font
# directory still wins.
if sys.platform == "win32":
    os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")

import pytest

from snipux import setup_desktop


@pytest.fixture(autouse=True)
def _isolated_config(tmp_path):
    """Point every test at a throwaway `config.json`, never this machine's
    real `~/.config/snipux/` (or `%APPDATA%`/`XDG_CONFIG_HOME` equivalent) --
    SNX-126.

    `setup_desktop.config_path()` is the one seam every `load_*`/`save_*`
    helper in that module already funnels through, `config_dir=None`
    meaning "the real location" -- so patching it here, autouse, isolates
    the whole suite in one place rather than requiring every test that
    happens to build an `OverlayWindow`/`Chooser`/`AppController` to
    remember to mock `load_kind`/`save_kind`/etc. itself. Before this, a
    real `~/.config/snipux/config.json` left over from actually running
    snipux (e.g. with `"kind": "record"` recorded by a prior session) was
    read straight into fresh test overlays, so the suite's result depended
    on how this machine last used the app -- the same class of bug SNX-109
    fixed for the global hotkey.

    An explicit `config_dir` (test_setup_desktop.py's own `run_setup`/
    `run_remove` calls, or anything passing `tmp_path` directly) is left
    alone -- only the "use the real default" case of `config_dir=None` is
    redirected, to a fresh directory scoped to *this* test's own `tmp_path`
    so no state leaks between tests either.

    Restored by hand in a `finally`, deliberately not via the `monkeypatch`
    fixture: pulling `monkeypatch` into an autouse conftest fixture would
    force it to be instantiated ahead of test-local fixtures that also
    request it (e.g. test_platform.py's `_restore_the_real_platform`,
    which relies on `monkeypatch` having already restored `sys.platform`
    by the time *it* tears down) -- flipping their teardown order relative
    to it and reintroducing exactly the kind of machine/order-dependent
    flakiness this ticket exists to remove.
    """
    fake_dir = tmp_path / "config"
    real_config_path = setup_desktop.config_path
    setup_desktop.config_path = (
        lambda config_dir=None: real_config_path(fake_dir if config_dir is None else config_dir)
    )
    try:
        yield
    finally:
        setup_desktop.config_path = real_config_path


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
