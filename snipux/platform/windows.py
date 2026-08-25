"""The Windows `Platform`: desktop integration mostly not implemented yet,
capture is (SNX-88), and the global capture hotkey is (SNX-91).

snipux is going cross-platform with Windows as the first target for full
parity (SNX-85), but most of the desktop-integration side of that hasn't
landed -- `install_desktop_integration`, `remove_desktop_integration`, and
`default_save_folder` still raise `UnimplementedPlatformError` naming
themselves and "Windows", rather than silently doing nothing or pretending
Linux's `.desktop`/gsettings mechanism means anything here. Filling those in
for real means giving each method a Windows-native implementation (a Start
Menu shortcut and Run-key entry in place of a `.desktop`/autostart file,
`%USERPROFILE%\\Pictures` in place of `~/Pictures`) -- against exactly the
interface `snipux/platform/__init__.py` defines, with no other module
needing to change.

`bind_shortcut`/`unbind_shortcut` (SNX-91) are the first two operations to
get a real Windows implementation. GNOME owns the key on Linux and invokes
`snipux --snip` itself once `bind_gnome_shortcut()` tells it to -- there is
no such service on Windows. An application registers its own hotkey with
Win32's `RegisterHotKey` and receives `WM_HOTKEY` in its own message loop
instead, which means *this* process must hold the registration for as long
as it runs (and Windows releases it the moment the process doesn't, clean
exit or not -- see `unbind_shortcut`). `HotkeyEventFilter` below is the
other half: the `QAbstractNativeEventFilter` `app.py` installs on the
`QApplication` to actually notice a registered hotkey firing, since
`RegisterHotKey(None, ...)` posts to this thread's message queue rather
than to any window of ours.

`ctypes` against `user32`, not a new dependency -- see `capture.py`'s
`Win32GdiBackend` for the same reasoning already applied to the capture
backend, and CLAUDE.md on why a fourth runtime dependency is a decision to
raise rather than a detail.

`build_capture_registry()` (SNX-86/88) is the other exception: it forwards
to `capture.build_windows_registry()`, the same way `LinuxPlatform` forwards
to `capture.build_linux_registry()` -- CLAUDE.md's one architectural rule
(grab the whole virtual desktop in one shot, then let the existing overlay
run selection against that frozen frame) is what makes capture itself no
different here than on Linux; only *how* the grab happens changes per
platform, and that logic lives in `capture.py` alongside the backends it
chooses between, not duplicated in this module. See `capture.py` for the
qt-native/Win32-GDI backends themselves.
"""

from __future__ import annotations

import ctypes
import sys
from pathlib import Path
from typing import Callable

from PyQt6.QtCore import QAbstractNativeEventFilter

from snipux import capture, setup_desktop
from snipux.capture import BackendRegistry

from . import Platform, UnimplementedPlatformError

_PLATFORM_NAME = "Windows"

# Win32 RegisterHotKey modifier flags (winuser.h) -- ctypes has no symbolic
# version of these built in, same as the structs capture.py already defines
# by hand for the same reason.
_MOD_ALT = 0x0001
_MOD_CONTROL = 0x0002
_MOD_SHIFT = 0x0004
_MOD_WIN = 0x0008
# Vista+: one WM_HOTKEY per press, not one per OS key-repeat tick while held.
_MOD_NOREPEAT = 0x4000

_WM_HOTKEY = 0x0312
_ERROR_HOTKEY_ALREADY_REGISTERED = 1409

# snipux only ever holds one hotkey registration at a time, so a single,
# fixed id is enough -- RegisterHotKey's id only has to be unique within
# this process, not system-wide.
_HOTKEY_ID = 1

# setup_desktop.normalise_shortcut() only ever emits these four canonical
# modifier names (see its own _MODIFIER_ORDER) -- a KeyError here would mean
# that contract broke, not that a user typed something unexpected.
_MODIFIER_FLAGS = {
    "Control": _MOD_CONTROL,
    "Alt": _MOD_ALT,
    "Shift": _MOD_SHIFT,
    "Super": _MOD_WIN,
}

# Named keys `setup_desktop.normalise_shortcut()` can hand back that don't
# reduce to a single character -- not exhaustive (see `_accelerator_to_win32`),
# just the ones the design's own conflict tables and recorder already name.
_VK_NAMES = {
    "Print": 0x2C,  # VK_SNAPSHOT
    "Insert": 0x2D,
    "Delete": 0x2E,
    "Home": 0x24,
    "End": 0x23,
    "PgUp": 0x21,
    "PgDown": 0x22,
    "Up": 0x26,
    "Down": 0x28,
    "Left": 0x25,
    "Right": 0x27,
    "Tab": 0x09,
    "Space": 0x20,
    "Escape": 0x1B,
    "Backspace": 0x08,
    "Return": 0x0D,
    "Enter": 0x0D,
}


def _accelerator_to_win32(shortcut: str) -> tuple[int, int] | None:
    """The canonical `Control+Alt+S` -> `(MOD_CONTROL | MOD_ALT |
    MOD_NOREPEAT, VK_S)`, the pair `RegisterHotKey` wants -- or `None` if
    `shortcut` names a key this module doesn't know how to translate.

    A single letter or digit needs no lookup table at all: Win32's
    VK_A..VK_Z and VK_0..VK_9 constants are numerically identical to
    `ord("A")`..`ord("Z")`/`ord("0")`..`ord("9")`, which is also why
    `setup_desktop.normalise_shortcut()` already uppercases a
    single-character key on the way in. Anything else -- a function key, or
    one of the handful of named keys in `_VK_NAMES` -- is looked up rather
    than guessed, so an unrecognised key is reported as unsupported instead
    of silently registering the wrong one.
    """
    normalised = setup_desktop.normalise_shortcut(shortcut)
    if normalised is None:
        return None

    *modifier_names, key = normalised.split("+")
    modifiers = _MOD_NOREPEAT
    for name in modifier_names:
        modifiers |= _MODIFIER_FLAGS[name]

    if len(key) == 1 and key.isalnum():
        return modifiers, ord(key.upper())
    if key in _VK_NAMES:
        return modifiers, _VK_NAMES[key]
    if key[:1] == "F" and key[1:].isdigit():
        n = int(key[1:])
        if 1 <= n <= 24:
            return modifiers, 0x70 + (n - 1)  # VK_F1..VK_F24
    return None


class _MSG(ctypes.Structure):
    """The Win32 `MSG` struct (winuser.h), trimmed to the fields
    `HotkeyEventFilter` actually reads -- ctypes has no symbolic version of
    this one either, same as capture.py's `_RECT`/`_BitmapInfoHeader`.
    """

    _fields_ = [
        ("hwnd", ctypes.c_void_p),
        ("message", ctypes.c_uint),
        ("wParam", ctypes.c_size_t),
        ("lParam", ctypes.c_ssize_t),
        ("time", ctypes.c_uint32),
        ("pt_x", ctypes.c_long),
        ("pt_y", ctypes.c_long),
    ]


class HotkeyEventFilter(QAbstractNativeEventFilter):
    """Watches every native Windows message Qt's event loop pumps for the
    one thing this process actually cares about: the `WM_HOTKEY` that fires
    when the combination `WindowsPlatform.bind_shortcut()` registered is
    pressed, and calls `on_triggered` when it does (SNX-91).

    `RegisterHotKey(None, ...)` -- a null window handle -- posts
    `WM_HOTKEY` to the *calling thread's* message queue rather than to any
    window, which is exactly what lets it fire while another application
    has focus: there is no window of ours involved, global or not. Qt's own
    Windows event dispatcher already pumps that queue -- it has to, to run
    the event loop at all -- and hands every message it sees to an
    installed `QAbstractNativeEventFilter` before its own handling, which is
    the only hook this process has into that queue; there is no Qt signal
    for "a native message arrived".

    A plain callback rather than a Qt signal: `app.py` constructs this
    before the object whose method it wants called (`AppController.
    start_capture`) has anywhere else to `connect()` it from.
    """

    def __init__(self, on_triggered: Callable[[], None]):
        super().__init__()
        self._on_triggered = on_triggered

    @staticmethod
    def is_available() -> bool:
        return sys.platform == "win32"

    def nativeEventFilter(self, eventType, message):
        if bytes(eventType) != b"windows_generic_MSG":
            return False, 0
        msg = ctypes.cast(int(message), ctypes.POINTER(_MSG)).contents
        if msg.message == _WM_HOTKEY:
            self._on_triggered()
        # Never consumes the message (False): WM_HOTKEY has no other
        # handler in this process, but deciding that is not this filter's
        # job -- Qt (and whatever else might also be filtering) still gets
        # to see it.
        return False, 0


class WindowsPlatform(Platform):
    def __init__(self):
        # None means "nothing currently registered" -- the state
        # bind_shortcut()/unbind_shortcut() both read and update, so a
        # rebind (Settings' Save button) knows to release the old
        # combination first rather than leaving it also held.
        self.registered_shortcut: str | None = None

    def install_desktop_integration(self, *, shortcut: str | None = None) -> int:
        raise UnimplementedPlatformError(_PLATFORM_NAME, "install_desktop_integration")

    def remove_desktop_integration(self) -> int:
        raise UnimplementedPlatformError(_PLATFORM_NAME, "remove_desktop_integration")

    def bind_shortcut(self, shortcut: str | None = None) -> str:
        """(Re)register the global hotkey via Win32's `RegisterHotKey` --
        Windows' own equivalent of `linux.py`'s GNOME custom-keybinding
        dance, except the registration is held by this process for as long
        as it runs rather than remembered by a desktop service (SNX-91).
        `shortcut` defaults to whatever `setup_desktop` remembers
        (`DEFAULT_SHORTCUT`, `Control+Alt+S`, the first time).

        Never raises: a combination already owned by another application is
        the expected, documented way `RegisterHotKey` fails, not a bug, and
        is reported back as a one-line, human-readable clash -- by the
        shortcut's own name, since Win32 has no way to ask *who* holds it,
        unlike GNOME's introspectable schemas -- rather than swallowed. That
        is the acceptance criterion this exists for; `self.registered_shortcut`
        staying `None` afterwards is how a caller (`app.py`) tells a real
        bind apart from one of these without parsing the message text.
        """
        if shortcut is None:
            shortcut = setup_desktop.load_shortcut()

        problem = setup_desktop.validate_shortcut(shortcut)
        if problem is not None:
            return f"Could not bind the shortcut: {problem}"

        translated = _accelerator_to_win32(shortcut)
        if translated is None:
            return (
                f"'{setup_desktop.human_shortcut(shortcut)}' is not a key "
                "combination snipux can register on Windows."
            )

        # Released first, always -- so rebinding to a new combination swaps
        # which one is held rather than leaving the previous registration
        # also grabbed, the same "splice, don't duplicate" care
        # bind_gnome_shortcut()/_append_slot() already take on Linux.
        self.unbind_shortcut()

        modifiers, vk = translated
        if not ctypes.windll.user32.RegisterHotKey(None, _HOTKEY_ID, modifiers, vk):
            error = ctypes.GetLastError()
            if error == _ERROR_HOTKEY_ALREADY_REGISTERED:
                return (
                    f"{setup_desktop.human_shortcut(shortcut)} is already in use by "
                    "another application -- snipux cannot use it too."
                )
            return (
                f"Could not bind {setup_desktop.human_shortcut(shortcut)} "
                f"(Windows error {error})."
            )

        self.registered_shortcut = shortcut
        return f"Bound {setup_desktop.human_shortcut(shortcut)} to start a snip."

    def unbind_shortcut(self) -> str:
        """The counterpart to `bind_shortcut()`: releases whatever this
        process currently holds, if anything.

        Also what a clean exit calls (`app.py` connects this to
        `QApplication.aboutToQuit`) -- though it is not what makes an
        *unclean* one safe. `RegisterHotKey` ties the registration to the
        calling thread, and Windows releases it the moment that thread (and
        so this single-threaded process) goes away, whether this ever runs
        or not -- which is the actual guarantee behind "a restart can
        re-register it", not this method.
        """
        if self.registered_shortcut is None:
            return "No snipux shortcut is currently registered."

        shortcut = self.registered_shortcut
        self.registered_shortcut = None
        if not ctypes.windll.user32.UnregisterHotKey(None, _HOTKEY_ID):
            return f"Could not release {setup_desktop.human_shortcut(shortcut)}."
        return f"Released {setup_desktop.human_shortcut(shortcut)}."

    def default_save_folder(self) -> Path:
        raise UnimplementedPlatformError(_PLATFORM_NAME, "default_save_folder")

    def build_capture_registry(self) -> BackendRegistry:
        return capture.build_windows_registry()
