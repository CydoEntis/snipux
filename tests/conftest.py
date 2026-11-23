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
import time
from pathlib import Path

# CLAUDE.md requires this suite to pass headless -- a build machine has no
# display, and neither does an agent's worktree -- so the QPA platform is
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

from snipux import ffmpeg, setup_desktop
from snipux.marks import session_styles
from snipux.platform import tesseract_ocr


@pytest.fixture(autouse=True)
def _fresh_tool_styles():
    """Every test starts from the first-run per-tool style.

    `marks.session_styles` outlives every window by design -- the style set
    on one snip is still set on the next -- which within a test run means it
    would also outlive every test, and one test's red pen would be the next
    one's.
    """
    session_styles.reset()
    try:
        yield
    finally:
        session_styles.reset()


@pytest.fixture(autouse=True)
def _fresh_design_caches():
    """Icons and resolved fonts are cached for the life of the application,
    which is the life of the whole run here; a test that swaps the font
    database or an icon file must not inherit the previous test's answer."""
    from snipux import design

    design.clear_caches()
    yield
    design.clear_caches()


@pytest.fixture(autouse=True)
def _no_system_ffmpeg():
    """Every test starts on a machine with no system ffmpeg.

    `snipux.ffmpeg.probe()` runs the real binary, which no test may do
    (CODE-STANDARDS.md), and caches the answer for the process -- so an
    unguarded probe would make the suite's result depend on what this
    machine has installed. A test that needs one says what it has, by
    monkeypatching `ffmpeg.probe`.
    """
    ffmpeg._probed = None
    try:
        yield
    finally:
        ffmpeg.reset()


@pytest.fixture(autouse=True)
def _no_system_tesseract():
    """Every test starts on a machine with no system tesseract.

    The same rule as `_no_system_ffmpeg`: on Linux, Hide sensitive is live
    wherever a `tesseract` is on `PATH`, so an overlay built by a test on a
    machine that has one would start the real engine, and the suite would
    answer differently here and on CI. A test that needs one says so, by
    monkeypatching `tesseract_ocr.find_binary` and passing a fake runner.

    Nor a package manager to name in the greyed switch's reason: looking
    for one is a `PATH` search every overlay built here would otherwise
    make, under whatever `sys.platform` its test has faked.

    Restored by hand for the reason `_isolated_config` gives.
    """
    real = tesseract_ocr.find_binary
    tesseract_ocr.find_binary = lambda: None
    tesseract_ocr._manager = None
    try:
        yield
    finally:
        tesseract_ocr.find_binary = real
        tesseract_ocr.reset()


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
    remember to mock `load_after_capture`/`save_last_region`/etc. itself.
    Before this, a real `~/.config/snipux/config.json` left over from
    actually running snipux was read straight into fresh test overlays, so
    the suite's result depended on how this machine last used the app -- the
    same class of bug SNX-109 fixed for the global hotkey.

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


# What a test must never be able to reach: this machine's own home. Read
# before any test runs, so the check in test_setup_desktop.py compares
# against the real one rather than against a fixture's.
REAL_HOME = Path.home()

# `USERPROFILE` is what `Path.home()` reads on Windows.
_HOME_VARIABLES = ("HOME", "USERPROFILE", "XDG_CONFIG_HOME", "XDG_DATA_HOME")


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path_factory):
    """Give every test a home of its own, so nothing it runs can write to
    this machine's.

    `_isolated_config` above covers `config.json` and nothing else.
    Everything else `setup_desktop` writes falls back to the real home
    when a caller passes no directory: the Hyprland bindings
    (`_hyprland_config_dir`), the `.desktop` and autostart entries, the
    icons, `~/Pictures/snipux`. A test that reached one of those by
    accident edited the developer's own desktop -- and one did: with
    `HYPRLAND_INSTANCE_SIGNATURE` set, a run of this suite took the
    Snipux shortcut out of `~/.config/hypr/bindings.lua` and reloaded
    Hyprland. CI has no Hyprland, so it never showed there.

    All three variables, because `XDG_*` unset means "under `$HOME`" and
    set means "here, whatever `$HOME` is". Not `XDG_CACHE_HOME`: nothing
    snipux writes goes there, and fontconfig would rebuild its cache.

    By hand rather than through `monkeypatch`, for the teardown-ordering
    reason `_isolated_config` gives.
    """
    # A folder beside the test's `tmp_path`, not inside it: plenty of tests
    # count what is in theirs.
    home = tmp_path_factory.mktemp("home")
    values = {
        "HOME": home,
        "USERPROFILE": home,
        "XDG_CONFIG_HOME": home / ".config",
        "XDG_DATA_HOME": home / ".local" / "share",
    }
    before = {name: os.environ.get(name) for name in _HOME_VARIABLES}
    for name, value in values.items():
        value.mkdir(parents=True, exist_ok=True)
        os.environ[name] = str(value)
    try:
        yield home
    finally:
        for name, value in before.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def disconnect_quit_time_unbind() -> None:
    """Take `platform.current.unbind_shortcut` off the application's
    `aboutToQuit`, however many times it was connected.

    `AppController.install_hotkey_listener()` connects it, and the
    `QApplication` is one object for the whole run -- so a test that
    installed the listener left the real unbind waiting on it, and the
    next test anywhere to quit the application ran it. That is how the
    shortcut above was removed: by a recording test that quits mid-stop,
    thousands of tests after the one that made the connection.
    """
    from PyQt6.QtCore import QCoreApplication

    from snipux import platform

    application = QCoreApplication.instance()
    if application is None:
        return
    # Both: the record shortcut's unbind is connected beside the snip one's.
    for name in ("unbind_shortcut", "unbind_record_shortcut"):
        while True:
            try:
                application.aboutToQuit.disconnect(getattr(platform.current, name))
            except (TypeError, RuntimeError, AttributeError):
                break   # not connected (any more)


@pytest.fixture(autouse=True)
def _no_omarchy_defaults():
    """Every test starts on a machine with no Omarchy default bindings.

    A Hyprland bind is refused when one of Omarchy's own binds already
    holds the key, and those are read from `/usr/share/omarchy`, which an
    Omarchy desk has and CI does not. A test that is about them points
    `_OMARCHY_BINDINGS_DIR` at a folder of its own.
    """
    real = setup_desktop._OMARCHY_BINDINGS_DIR
    setup_desktop._OMARCHY_BINDINGS_DIR = Path("no-omarchy-bindings-here")
    try:
        yield
    finally:
        setup_desktop._OMARCHY_BINDINGS_DIR = real


@pytest.fixture(autouse=True)
def _no_real_record_shortcut():
    """Every test starts with the record shortcut's bind and unbind inert
    on `platform.current`.

    The controller binds it wherever it binds the snip shortcut -- at
    startup on Windows, on Save in Settings, on an upgrade -- and the many
    tests that stub `bind_shortcut` to keep off the real OS were written
    before there was a second one to stub. Left real, each of them would
    make a genuine `RegisterHotKey` on a Windows runner, or write a
    Hyprland bind and reload the compositor on a developer's desk.

    Only the instance `platform.current`: a test of the real thing builds
    its own `LinuxPlatform()`/`WindowsPlatform()`, as test_platform.py
    does, or sets its own stub over this one. By hand rather than through
    `monkeypatch`, for the teardown-ordering reason `_isolated_config`
    gives.
    """
    from snipux import platform

    current = platform.current
    names = ("bind_record_shortcut", "unbind_record_shortcut", "registered_record_shortcut")
    before = {name: current.__dict__[name] for name in names if name in current.__dict__}
    current.bind_record_shortcut = lambda shortcut=None: ""
    current.unbind_record_shortcut = lambda: ""
    current.registered_record_shortcut = setup_desktop.DEFAULT_RECORD_SHORTCUT
    try:
        yield
    finally:
        for name in names:
            current.__dict__.pop(name, None)
        current.__dict__.update(before)


@pytest.fixture(autouse=True)
def _no_unbind_outlives_its_test():
    try:
        yield
    finally:
        disconnect_quit_time_unbind()


class FakeRegistry:
    """An in-memory stand-in for the slice of `winreg` that
    `platform/windows.py` uses, so no test can write this machine's real
    registry and the same tests run on Linux, which has no `winreg` at all.

    `keys` maps a lower-cased key path to its values, as the registry
    treats key names case-insensitively. `DeleteKey` refuses a key with
    subkeys, as the real one does.
    """

    HKEY_CURRENT_USER = "HKCU"
    KEY_WRITE = 0x20006
    KEY_SET_VALUE = 0x0002
    REG_SZ = 1

    def __init__(self):
        self.keys: dict[str, dict[str, object]] = {}

    class _Handle:
        def __init__(self, path):
            self.path = path

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def CreateKeyEx(self, root, path, reserved=0, access=0):
        parts = path.lower().split("\\")
        for depth in range(1, len(parts) + 1):
            self.keys.setdefault("\\".join(parts[:depth]), {})
        return self._Handle(path.lower())

    def OpenKey(self, root, path, reserved=0, access=0):
        if path.lower() not in self.keys:
            raise FileNotFoundError(path)
        return self._Handle(path.lower())

    def QueryInfoKey(self, handle):
        depth = handle.path.count("\\") + 1
        subkeys = sum(
            1
            for key in self.keys
            if key.startswith(handle.path + "\\") and key.count("\\") == depth
        )
        return subkeys, len(self.keys[handle.path]), 0

    def SetValueEx(self, handle, name, reserved, kind, data):
        self.keys[handle.path][name] = data

    def DeleteValue(self, handle, name):
        if name not in self.keys[handle.path]:
            raise FileNotFoundError(name)
        del self.keys[handle.path][name]

    def DeleteKey(self, root, path):
        path = path.lower()
        if path not in self.keys:
            raise FileNotFoundError(path)
        if any(key.startswith(path + "\\") for key in self.keys):
            raise PermissionError(f"{path} has subkeys")
        del self.keys[path]

    def value(self, path, name):
        return self.keys.get(path.lower(), {}).get(name)


@pytest.fixture(autouse=True)
def fake_registry():
    """Every test gets `FakeRegistry` in place of `winreg`, and Explorer is
    never told about an association change. Restored by hand for the same
    reason `_isolated_config` is."""
    from snipux.platform import windows

    registry = FakeRegistry()
    real_winreg = windows.winreg
    real_announce = windows._announce_association_change
    windows.winreg = registry
    windows._announce_association_change = lambda: None
    try:
        yield registry
    finally:
        windows.winreg = real_winreg
        windows._announce_association_change = real_announce


@pytest.fixture(autouse=True)
def _restore_excepthook():
    """Put `sys.excepthook` back after any test that replaced it.

    Several tests really run `app._become_resident`, which installs the
    crash log as the hook. Left in place it would outlive that test, and
    every later exception inside a Qt handler would become a line in a
    temp-dir log instead of the abort that fails the run -- the suite would
    pass over exactly the class of bug the hook exists to survive.

    Restored by hand rather than through `monkeypatch`, for the teardown
    ordering reason `_isolated_config` gives above.
    """
    hook = sys.excepthook
    try:
        yield
    finally:
        sys.excepthook = hook


@pytest.fixture(autouse=True)
def _no_save_outlives_its_test():
    """Let a background save a test started report back before the next
    test begins.

    `output.save_image_later` answers on the event loop, some milliseconds
    after the overlay that asked has closed. A test that ends first would
    leave that answer to arrive in the middle of a later one -- adding a
    file to its Recent list, or raising a notice it then counts.
    """
    yield
    from PyQt6.QtTest import QTest

    from snipux import output

    deadline = time.monotonic() + 10
    while output.saves_in_flight():
        assert time.monotonic() < deadline, "a background save never reported back"
        QTest.qWait(1)


@pytest.fixture(autouse=True)
def _fresh_watermark_session():
    """Start every test with a fresh launch's watermark: off, bottom right,
    70%.

    The stills bar's watermark lasts a session (#69), and in a test run the
    session is every test in the process, so a test that switched it on
    would stamp the exports of every test after it. Looked up rather than
    imported: this module must not be what first imports the overlay.
    """
    try:
        yield
    finally:
        overlay = sys.modules.get("snipux.overlay")
        if overlay is not None:
            overlay.watermark_session = overlay.WatermarkChoice()


@pytest.fixture(autouse=True)
def _fresh_copy_history():
    """Start every test with an empty copy history and no one listening.

    Like the watermark above, the history lasts the process, which in a
    test run is every test -- and the listener is whichever controller was
    built last, which by the next test is one nobody is holding any more.
    """
    try:
        yield
    finally:
        output = sys.modules.get("snipux.output")
        if output is not None:
            output.set_copy_history_listener(None)
            output._copy_history.clear()


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
