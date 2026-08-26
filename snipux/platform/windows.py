"""The Windows `Platform`: desktop integration (SNX-92), capture (SNX-88),
and the global capture hotkey (SNX-91). `default_save_folder` is still not
implemented -- it raises `UnimplementedPlatformError` naming itself and
"Windows", rather than silently doing nothing or pretending Linux's
`~/Pictures` means anything here.

`install_desktop_integration`/`remove_desktop_integration` (SNX-92) are
Windows-native rather than a pretense of Linux's `.desktop`/gsettings
mechanism: a Start Menu shortcut and a second copy of it in the per-user
Startup folder (see `install_desktop_integration`'s own docstring for why
that and not the Run registry key), both `.lnk` files built through COM's
`IShellLinkW`/`IPersistFile` -- the actual mechanism behind every Windows
shortcut -- and both pointing their icon at a `.ico` built at setup time
from the same vendored PNGs `install_icons()` already copies into Linux's
hicolor theme (`setup_desktop.render_ico()`). Implemented against exactly
the interface `snipux/platform/__init__.py` defines, with no other module
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

`find_shortcut_conflict` (SNX-93) is the Windows answer to
`setup_desktop.find_shortcut_conflicts_named()`'s GNOME-only conflict check:
Settings' banner and Save button call it instead, on Windows, to name the
Windows Snipping Tool's own Win+Shift+S or whatever else `RegisterHotKey`
itself refuses -- see its own docstring for why a probe registration is the
only way to see the latter at all.

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
qt-native/Win32-GDI backends themselves. `build_recording_registry()`
(SNX-119) forwards to `recording.build_windows_registry()` for the same
reason.

`reattach_console()` (SNX-100) is unrelated to any of the above: it is
what lets `packaging/windows/snipux.spec` build a *windowed* snipux.exe --
no console popping up behind the tray on every launch -- while
`--list-backends`/`--setup`/`--remove`/`--snip` still print when actually
run from a terminal. `app.py`'s `cli()` calls it before anything else, the
same "this is the one seam" reasoning as every other operation here, done
through a bare function rather than a `Platform` method because it has to
run before `platform.current` is of any use to anyone -- it is about
whether `print()` itself works yet, not about picking an implementation.

`_ensure_stable_copy()`/`ensure_stable_install()` (SNX-103) are what make
the portable `snipux.exe` safe to distribute at all: it is the *only*
Windows distribution route now that the Inno Setup installer is gone
(SNX-104 -- Smart App Control blocked it outright, with no way to click
through; see the README's Smart App Control section), and until now
nothing stopped its Start Menu/Startup shortcuts from pointing at
wherever the user happened to double-click it from -- typically
Downloads, which most people clean out sooner or later.
`_ensure_stable_copy()` relocates a running portable build to
`_portable_exe_path()` (the same `%LOCALAPPDATA%\\snipux` directory
`_icon_path()` already writes into) before
`install_desktop_integration()` ever points a shortcut at anything, and
`ensure_stable_install()` is the `Platform` hook `app._become_resident()`
calls on *every* launch that becomes resident -- not gated behind
`run_first_launch_setup()`'s one-time record, since a newer download run
over an older install still has to replace it, and that record has
already been set from the first launch onward. Both are no-ops off a
portable build (`sys.frozen` unset, e.g. a pip/pipx install or a source
checkout), which is what keeps this off the one case the ticket says must
be untouched.
"""

from __future__ import annotations

import ctypes
import os
import shutil
import sys
from pathlib import Path
from typing import Callable

from PyQt6.QtCore import QAbstractNativeEventFilter

from snipux import capture, recording, setup_desktop
from snipux.capture import BackendRegistry
from snipux.recording import RecorderRegistry

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

# find_shortcut_conflict()'s own id, distinct from _HOTKEY_ID above: the
# probe registers and immediately releases a *candidate* combination to see
# whether Windows refuses it, and must never touch the real, held
# registration bind_shortcut() already owns under _HOTKEY_ID while doing so.
_CONFLICT_PROBE_ID = 2

# The Windows Snipping Tool's own global shortcut (Windows 10 1809+) --
# already spoken for on every fresh install, whether or not anything else
# has claimed a hotkey yet, and invisible to a RegisterHotKey probe since it
# is a shell feature, not a process to collide with. In canonical form
# (_MODIFIER_ORDER puts Shift before Super), not the "Win+Shift+S" spelling
# Microsoft's own docs use.
_SNIPPING_TOOL_SHORTCUT = "Shift+Super+S"

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


class _GUID(ctypes.Structure):
    """The Win32 `GUID` struct (guiddef.h) -- ctypes has no symbolic
    version of this one either, same as every other struct hand-rolled in
    this file and capture.py. Used only to address the two COM classes/
    interfaces `_create_shortcut` below needs (`CLSID_ShellLink`,
    `IID_IShellLinkW`, `IID_IPersistFile`); nothing here parses an
    arbitrary GUID at runtime.
    """

    _fields_ = [
        ("data1", ctypes.c_uint32),
        ("data2", ctypes.c_uint16),
        ("data3", ctypes.c_uint16),
        ("data4", ctypes.c_uint8 * 8),
    ]


def _guid(literal: str) -> _GUID:
    """`"{00021401-0000-0000-C000-000000000046}"` -> the `_GUID` struct
    `CoCreateInstance`/`QueryInterface` want. Only ever called on the three
    literals below, which is why this doesn't validate its input -- a
    malformed one is a bug in this file, not something to handle politely.
    """
    hexed = literal.strip("{}").replace("-", "")
    data4 = (ctypes.c_uint8 * 8)(*bytes.fromhex(hexed[16:32]))
    return _GUID(int(hexed[0:8], 16), int(hexed[8:12], 16), int(hexed[12:16], 16), data4)


# CLSID_ShellLink and the two interfaces `_create_shortcut` queries for
# (shobjidl_core.h/objidl.h) -- COM identifies classes and interfaces by
# GUID, not name, and ctypes has no registry of either.
_CLSID_SHELL_LINK = "{00021401-0000-0000-C000-000000000046}"
_IID_ISHELLLINKW = "{000214F9-0000-0000-C000-000000000046}"
_IID_IPERSISTFILE = "{0000010B-0000-0000-C000-000000000046}"
_CLSCTX_INPROC_SERVER = 0x1

# IShellLinkW/IPersistFile vtable slot indices -- the offsets `_com_call`
# below reads a function pointer out of, in the order shobjidl_core.h and
# objidl.h declare them. Every COM interface is-a IUnknown, so slot 0 is
# always QueryInterface and slot 2 is always Release, on both interfaces;
# the rest are IShellLinkW's or IPersistFile's own methods, and only the
# handful this module actually calls are named here -- there is no
# vtable/interface concept in ctypes to read the rest from, so an unused
# slot has no reason to be enumerated.
_QUERY_INTERFACE = 0
_RELEASE = 2
_ISHELLLINKW_SET_DESCRIPTION = 7
_ISHELLLINKW_SET_ICON_LOCATION = 17
_ISHELLLINKW_SET_PATH = 20
_IPERSISTFILE_SAVE = 6


def _com_call(interface: ctypes.c_void_p, index: int, restype, *argtypes):
    """Read the `index`th function pointer out of `interface`'s vtable and
    wrap it as a callable -- ctypes can call a COM method (it's just
    another native function, stdcall on x86 and the one true convention on
    x64, exactly what `WINFUNCTYPE` builds) but has no notion of interfaces
    or virtual dispatch to do it *through*, so this is that dispatch done
    by hand: `interface` -> its vtable pointer (the struct's first field,
    per COM's memory layout) -> the `index`th entry in that vtable.

    The returned callable still takes `interface` itself as its first
    argument (`argtypes` here never includes it) -- the same "self" every
    one of these methods implicitly takes in C++, made explicit because
    ctypes has no bound-method concept for a raw function pointer either.
    """
    vtable = ctypes.cast(interface, ctypes.POINTER(ctypes.c_void_p))[0]
    function_ptr = ctypes.cast(vtable, ctypes.POINTER(ctypes.c_void_p))[index]
    return ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)(function_ptr)


def _create_shortcut(
    lnk_path: Path, target: Path, *, icon_path: Path | None = None, description: str = ""
) -> bool:
    """Write a Windows `.lnk` shortcut at `lnk_path` that launches `target`
    with no arguments -- the same bare, argument-less launch Linux's own
    `Exec=` line does (`setup_desktop.render_desktop_entry`) -- via COM's
    `IShellLinkW`/`IPersistFile`, the actual mechanism behind every Windows
    shortcut: a `.lnk` is a serialised COM object, not a text file the way
    Linux's `.desktop` is. Reached through bare `ctypes` rather than
    `pywin32`/`comtypes`: CLAUDE.md already rules out a fourth runtime
    dependency for a screenshot tool, and this needs three method calls on
    one COM object, not a general-purpose COM binding.

    Never raises. Every failure -- `CoCreateInstance` refusing the class,
    `QueryInterface` refusing `IPersistFile`, `Save` itself failing (e.g. a
    read-only Start Menu folder) -- is a plain `False`, exactly like a
    setup_desktop.py step that can't run; the caller (`_write_shortcut`)
    is what turns that into a reported note rather than a crash.
    """
    ole32 = ctypes.windll.ole32
    ole32.CoInitialize(None)
    try:
        shell_link = ctypes.c_void_p()
        hresult = ole32.CoCreateInstance(
            ctypes.byref(_guid(_CLSID_SHELL_LINK)),
            None,
            _CLSCTX_INPROC_SERVER,
            ctypes.byref(_guid(_IID_ISHELLLINKW)),
            ctypes.byref(shell_link),
        )
        if hresult != 0 or not shell_link:
            return False
        try:
            set_path = _com_call(shell_link, _ISHELLLINKW_SET_PATH, ctypes.c_long, ctypes.c_wchar_p)
            if set_path(shell_link, str(target)) != 0:
                return False

            if description:
                set_description = _com_call(
                    shell_link, _ISHELLLINKW_SET_DESCRIPTION, ctypes.c_long, ctypes.c_wchar_p
                )
                set_description(shell_link, description)
            if icon_path is not None:
                set_icon = _com_call(
                    shell_link, _ISHELLLINKW_SET_ICON_LOCATION, ctypes.c_long, ctypes.c_wchar_p, ctypes.c_int
                )
                set_icon(shell_link, str(icon_path), 0)

            query_interface = _com_call(
                shell_link,
                _QUERY_INTERFACE,
                ctypes.c_long,
                ctypes.POINTER(_GUID),
                ctypes.POINTER(ctypes.c_void_p),
            )
            persist_file = ctypes.c_void_p()
            hresult = query_interface(
                shell_link, ctypes.byref(_guid(_IID_IPERSISTFILE)), ctypes.byref(persist_file)
            )
            if hresult != 0 or not persist_file:
                return False
            try:
                save = _com_call(
                    persist_file, _IPERSISTFILE_SAVE, ctypes.c_long, ctypes.c_wchar_p, ctypes.c_int
                )
                return save(persist_file, str(lnk_path), 1) == 0
            finally:
                _com_call(persist_file, _RELEASE, ctypes.c_ulong)(persist_file)
        finally:
            _com_call(shell_link, _RELEASE, ctypes.c_ulong)(shell_link)
    finally:
        ole32.CoUninitialize()


def _start_menu_dir() -> Path:
    """The per-user Start Menu `Programs` folder -- what a shortcut has to
    be written into for Windows to list it as a Start Menu entry at all.
    `%APPDATA%` (`Roaming`, not `Local`) is where Windows itself puts this,
    same as every other per-user shell folder without a dedicated
    `os.environ` entry of its own.
    """
    return Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs"


def _startup_dir() -> Path:
    """The per-user Startup folder: a subfolder of `_start_menu_dir()` that
    Windows runs the shortcut of every `.lnk` in at login -- see
    `WindowsPlatform.install_desktop_integration` for why this, and not the
    Run registry key, is what snipux uses for autostart.
    """
    return _start_menu_dir() / "Startup"


def _local_app_data_dir() -> Path:
    """`%LOCALAPPDATA%`, or its fallback when unset -- the one place this
    module reads that environment variable, shared by `_icon_path()` and
    `_portable_exe_path()` (SNX-103) rather than each guessing it
    separately.
    """
    return Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))


def _icon_path() -> Path:
    """Where the `.ico` built from the vendored PNGs
    (`setup_desktop.render_ico`) is written -- `%LOCALAPPDATA%\\snipux`,
    the Windows analogue of the `hicolor` icon theme's
    `XDG_DATA_HOME/icons/hicolor` on Linux: a per-user, `Roaming`-excluded
    (this is regenerable, not something a roaming profile needs to carry
    around) cache of something built from files already vendored in the
    package.
    """
    return _local_app_data_dir() / "snipux" / "snipux.ico"


def _portable_exe_path() -> Path:
    """SNX-103: the stable, per-user home a portable `snipux.exe` copies
    itself to on first run -- the same `%LOCALAPPDATA%\\snipux` directory
    `_icon_path()` already writes the generated `.ico` into, not a second
    per-user cache location invented for this.

    Named `snipux.exe` unconditionally, never after whatever the download
    happened to be called (`snipux-1.4.0-portable.exe`, `snipux (1).exe`,
    ...) -- a fixed filename is what makes `_ensure_stable_copy()`
    *overwrite* this file on a later run rather than leave two versions
    sitting side by side, which is the whole of what makes "a newer
    version replaces an older install" true: nothing has to notice or
    clean up the old one, because there never is one once the copy lands.
    """
    return _local_app_data_dir() / "snipux" / "snipux.exe"


def _ensure_stable_copy() -> Path | None:
    """SNX-103: if this process is a self-contained, portable `snipux.exe`
    (PyInstaller's own `sys.frozen` marker -- the same check
    `setup_desktop.find_console_script()` already makes first, and for the
    same reason: a build like this has no separate console-script wrapper
    to point at, it *is* the thing to point at), copy it to
    `_portable_exe_path()` and return that path -- so a caller building a
    shortcut points it at a location durable against the user cleaning out
    Downloads, rather than at wherever this process happened to be run
    from.

    Returns `None` when there is nothing to relocate: this is not a
    portable build at all (`sys.frozen` unset -- a `pip`/`pipx` install or
    a source checkout, both already in a stable location a package
    manager, not this function, is responsible for), which is exactly the
    "none of this happens for a pip or pipx install" acceptance criterion
    -- the check below is the first thing this function does, before it
    touches the filesystem at all.

    A no-op, returning the already-stable path without copying anything,
    when this process is already running from `_portable_exe_path()` --
    the ordinary case once installed, since every shortcut
    `install_desktop_integration()` writes points there. Also a no-op,
    for the same reason but cheaper to check, when whatever already sits
    at the target is the same size as this process's own executable: two
    different builds of a PyQt6-bundling, multi-megabyte single-file exe
    matching in size by coincidence is not a real risk worth a slower,
    full-content comparison on every launch, and it is what stops a
    portable build re-run from its original download location (rather
    than from the shortcut this function already pointed at the copy)
    from re-copying dozens of megabytes to itself on every single launch.

    Otherwise copies over whatever is already at the target -- most often
    an older version's copy -- rather than refusing or renaming around it:
    combined with `_portable_exe_path()`'s fixed filename, this is what
    makes running a newer download replace an older install instead of
    the two ever coexisting.

    Never raises. A copy that fails -- a read-only target directory, or
    the target file still locked by an older version of snipux that is
    still running under it -- is a note, not a fatal error, the same "a
    step that can't run is reported, not crashed on" rule every other step
    in this module follows; the caller falls back to wherever this
    process was actually launched from, exactly as if this function did
    not exist.
    """
    if not getattr(sys, "frozen", False):
        return None

    current = Path(sys.executable).resolve()
    target = _portable_exe_path()

    try:
        if target.exists() and (
            current.samefile(target) or target.stat().st_size == current.stat().st_size
        ):
            return target
    except OSError:
        pass

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(current, target)
    except OSError as exc:
        print(f"Note: could not copy snipux to {target}: {exc}", file=sys.stderr)
        return None

    print(f"snipux copied to {target}")
    return target


def _remove_stable_copy(exe_path: Path) -> bool:
    """The counterpart to `_ensure_stable_copy()` (SNX-103): removes the
    portable build's relocated copy, the exact "report plainly, don't
    fail on an already-gone file" shape `_remove_icon()` above already
    uses. Deleting a file this very process may currently be running as
    (`snipux --remove` invoked from the relocated copy itself) is
    ordinarily still allowed by Windows -- unlinking a running exe's
    directory entry does not require the still-mapped image underneath it
    to be unlocked -- but any refusal is still a note here, not raised,
    the same as every other removal step in this module.
    """
    if not exe_path.exists():
        print(f"{exe_path} not found -- nothing to remove")
        return True
    try:
        exe_path.unlink()
    except OSError as exc:
        print(f"Note: could not remove {exe_path}: {exc}")
        return False
    print(f"Removed {exe_path}")
    return True


def _write_icon(icon_path: Path) -> bool:
    """Build the `.ico` (`setup_desktop.render_ico`) and write it to
    `icon_path`, reporting either way -- the Windows analogue of
    `setup_desktop.install_icons()`. A missing/unusable vendored PNG or an
    unwritable directory is a note, not a fatal error: the shortcuts below
    still work without a custom icon, just with a generic one.
    """
    data = setup_desktop.render_ico()
    if data is None:
        print("Note: no vendored icon found -- shortcuts will use a generic icon.")
        return False
    try:
        icon_path.parent.mkdir(parents=True, exist_ok=True)
        icon_path.write_bytes(data)
    except OSError as exc:
        print(f"Note: could not write the icon to {icon_path}: {exc}")
        return False
    print(f"Icon written to {icon_path}")
    return True


def _remove_icon(icon_path: Path) -> bool:
    """The counterpart to `_write_icon()`. Reports plainly, rather than
    failing, when there is nothing to remove -- a second `--remove`, or a
    first one run where `--setup` never wrote an icon, must not be treated
    as an error just because the file is already gone.
    """
    if not icon_path.exists():
        print(f"Icon not found at {icon_path} -- nothing to remove")
        return True
    try:
        icon_path.unlink()
    except OSError as exc:
        print(f"Note: could not remove the icon at {icon_path}: {exc}")
        return False
    print(f"Icon removed from {icon_path}")
    return True


def _write_shortcut(lnk_path: Path, target: Path, icon_path: Path | None, label: str) -> bool:
    """Write a `label` shortcut at `lnk_path` launching `target`, reporting
    either way -- the Windows analogue of `setup_desktop._write_entry()`.
    The filename never changes between runs, so a second `--setup`
    overwrites the first shortcut rather than adding a duplicate, the same
    "no extra bookkeeping needed" property `_write_entry()` has on Linux.

    `description="Snipux"` (SNX-107) is the `.lnk`'s own display-text field
    -- the COM analogue of the `.desktop` entry's `Name=` -- so the shortcut
    reads as the product name without touching `lnk_path`'s filename, which
    a second `--setup` still has to find unchanged to overwrite rather than
    duplicate.
    """
    try:
        lnk_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f"Note: could not create {lnk_path.parent} for the {label} entry: {exc}")
        return False
    if not _create_shortcut(lnk_path, target, icon_path=icon_path, description="Snipux"):
        print(f"Note: could not write the {label} entry at {lnk_path}.")
        return False
    print(f"{label} entry written to {lnk_path} (target={target})")
    return True


def _remove_shortcut(lnk_path: Path, label: str) -> bool:
    """The counterpart to `_write_shortcut()`, mirroring
    `setup_desktop._remove_entry()`: reports plainly, rather than failing,
    when there is nothing to remove.
    """
    if not lnk_path.exists():
        print(f"{label} entry not found at {lnk_path} -- nothing to remove")
        return True
    try:
        lnk_path.unlink()
    except OSError as exc:
        print(f"Note: could not remove the {label} entry at {lnk_path}: {exc}")
        return False
    print(f"{label} entry removed from {lnk_path}")
    return True


# AttachConsole's own pseudo-process-id (winbase.h: `((DWORD)-1)`) meaning
# "the console of whatever process started this one", not a real pid to
# look up.
_ATTACH_PARENT_PROCESS = -1

# The one AttachConsole failure that means "this process already has a
# console of its own" (GetLastError, winerror.h) rather than "there was
# nothing to attach to" -- see reattach_console()'s own docstring for why
# that distinction is the whole point.
_ERROR_ACCESS_DENIED = 5


def reattach_console() -> None:
    """SNX-100: let a *windowed*-subsystem snipux.exe (see
    `packaging/windows/snipux.spec`'s own comment on why it is built that
    way rather than `console=True`) still print to a terminal it was
    actually launched from, while never popping a console window of its
    own when it wasn't.

    A windowed-subsystem process is never handed its parent's console just
    because the parent happens to have one -- unlike a console-subsystem
    process (plain `python.exe`, or the pip-installed `snipux` console
    script), which inherits one automatically. That is exactly why a
    windowed build is silent when double-clicked from Explorer (nothing to
    inherit -- Explorer has no console either) but *also* silent when run
    as `snipux --list-backends` from a terminal, unless it asks that
    terminal for its console back itself. `AttachConsole(
    ATTACH_PARENT_PROCESS)` is that ask: it succeeds when whatever started
    this process had a console (a terminal), and fails when it didn't
    (Explorer, a Start Menu/Startup shortcut) -- which is also how this
    tells the two cases apart, rather than guessing from `sys.argv`.

    `sys.stdout`/`sys.stderr` are reopened against the newly-attached
    console's `CONOUT$` device on success -- PyInstaller's windowed
    bootloader starts both as `None` (there was no console to hand them a
    handle to yet), so a bare `print()` before this point would already
    have crashed with an `AttributeError`, not merely gone missing.

    Failing to attach is not reported as an error: it means precisely "no
    console was available", the double-click/shortcut case this whole
    function exists to keep silent. `sys.stdout`/`sys.stderr` are
    still pointed at `os.devnull` in that case, not left as `None` --
    setup_desktop.py and this module both fall back to a plain `print()`
    for dozens of "here's what happened" notes, and every one of those
    must have somewhere harmless to write instead of crashing the first
    time it runs with nothing attached.

    A no-op everywhere but Windows, and a no-op on Windows too when this
    process already has a console of its own (`ERROR_ACCESS_DENIED`,
    e.g. `python -m snipux`, or the pip-installed console script, both of
    which start with a real, already-working console inherited the
    ordinary way) -- `sys.stdout`/`sys.stderr` are already good streams
    there, and reopening them would be redundant at best.
    """
    if sys.platform != "win32":
        return

    if ctypes.windll.kernel32.AttachConsole(_ATTACH_PARENT_PROCESS):
        sys.stdout = open("CONOUT$", "w", encoding="utf-8", errors="replace", buffering=1)
        sys.stderr = open("CONOUT$", "w", encoding="utf-8", errors="replace", buffering=1)
        return

    if ctypes.GetLastError() == _ERROR_ACCESS_DENIED:
        return

    sys.stdout = open(os.devnull, "w")
    sys.stderr = open(os.devnull, "w")


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
        """`snipux --setup` on Windows (SNX-92): a Start Menu shortcut, a
        second copy of it in the per-user Startup folder, and a `.ico`
        built from the vendored PNGs for both to point at -- the Windows
        analogue of `LinuxPlatform`'s `.desktop`/autostart/hicolor trio.

        Startup-folder shortcut, not the Run registry key, for autostart:
        it is a plain file, inspectable in Explorer and removable by
        deleting it, the same properties that make Linux's autostart
        `.desktop` file (rather than some registry-like GNOME setting)
        the right analogue to reuse -- and it lets `_write_shortcut()`
        below be called twice with the same arguments rather than needing
        a second, registry-flavoured code path for what is otherwise the
        exact same step.

        `shortcut`, if given, is validated and remembered (like Linux's
        `run_setup`) but not bound here -- Windows has no persistent,
        setup-time keybinding mechanism the way GNOME's custom-keybindings
        are; `bind_shortcut()` (SNX-91) registers it fresh, in-process,
        every time snipux actually starts, so that is when it takes effect
        and this only has to make sure the right value is on record for it
        to read.

        This used to end by printing "<shortcut> will be registered the
        next time snipux starts" -- true of a bare `snipux --setup` run,
        which never holds a `RegisterHotKey` registration of its own, but
        false of the one caller that actually matters (SNX-101):
        `AppController.run_first_launch_setup()` calls this from the
        already-resident process, and only after `install_hotkey_listener()`
        has already bound the shortcut in that same process -- so the
        shortcut this note claimed was still pending had, in fact, already
        been working since before this function was even called. Printing
        it anyway was also a console line a windowed build has no console
        to show (SNX-100), so it was silent noise on top of being wrong.
        This function says nothing about the shortcut at all now;
        `bind_shortcut()`'s own return value, surfaced through
        `AppController._report_shortcut()` (the tray, or Settings), is the
        one place that ever reports whether it actually took.

        Returns 1 (and prints why, to stderr) only when the console script
        itself can't be found -- every other step can still report its own
        outcome without it, mirroring `setup_desktop.run_setup()`'s own
        "one missing prerequisite is fatal, everything else is a note" split.

        `_ensure_stable_copy()` (SNX-103) runs next, before either
        shortcut is written: a portable `snipux.exe` relocates itself to
        `_portable_exe_path()` at this point, and the shortcuts below are
        pointed at that stable copy rather than at `exec_path` itself
        whenever relocation actually happened -- which is also what
        `ensure_stable_install()` (called separately, once per launch, by
        `app._become_resident()`) has usually already done by the time
        this runs, so the second call here is normally the cheap,
        already-in-place no-op `_ensure_stable_copy()`'s own docstring
        describes. A no-op for anything that isn't a portable build (a
        pip/pipx install, a source checkout), which is what leaves
        `exec_path` -- the console script `find_console_script()` found --
        untouched for those.
        """
        if shortcut is not None:
            problem = setup_desktop.validate_shortcut(shortcut)
            if problem is not None:
                print(f"error: {problem}", file=sys.stderr)
                return 1
            if not setup_desktop.save_shortcut(shortcut):
                print(
                    f"Note: could not remember {setup_desktop.human_shortcut(shortcut)} -- "
                    "it will be bound this run, but a future --setup will revert it.",
                    file=sys.stderr,
                )

        exec_path = setup_desktop.find_console_script()
        if exec_path is None:
            print(
                "error: could not locate the installed snipux executable -- "
                "is snipux actually installed (pip install), rather than just "
                "being run from a checkout?",
                file=sys.stderr,
            )
            return 1

        stable_copy = _ensure_stable_copy()
        if stable_copy is not None:
            exec_path = stable_copy

        icon_path = _icon_path()
        has_icon = _write_icon(icon_path)

        _write_shortcut(
            _start_menu_dir() / "snipux.lnk", exec_path, icon_path if has_icon else None, "Start Menu"
        )
        _write_shortcut(
            _startup_dir() / "snipux.lnk", exec_path, icon_path if has_icon else None, "Startup"
        )

        return 0

    def remove_desktop_integration(self) -> int:
        """The exact counterpart to `install_desktop_integration()`:
        deletes the Start Menu shortcut, the Startup shortcut, the
        generated `.ico`, the relocated portable-build copy (SNX-103), and
        the remembered shortcut choice -- mirroring `setup_desktop.
        run_remove()` step for step. Always returns 0, the same reasoning
        `run_remove()` states for its own return: every step already
        reports its own failure or absence as a note rather than raising.

        `_portable_exe_path()` is attempted unconditionally, the same as
        the icon and both shortcuts above it -- it reports "nothing to
        remove" and moves on when there was never a portable copy to
        begin with (a pip/pipx install, a source checkout), rather than
        this method first checking `sys.frozen` to decide whether to try.
        """
        _remove_shortcut(_start_menu_dir() / "snipux.lnk", "Start Menu")
        _remove_shortcut(_startup_dir() / "snipux.lnk", "Startup")
        _remove_icon(_icon_path())
        _remove_stable_copy(_portable_exe_path())
        if setup_desktop.forget_shortcut():
            print(f"Removed {setup_desktop.config_path()}.")

        return 0

    def ensure_stable_install(self) -> Path | None:
        """SNX-103: `Platform`'s optional hook (see its own docstring for
        why this one isn't part of the required six), overridden here to
        forward to `_ensure_stable_copy()`.

        `app._become_resident()` calls this once, on every launch that
        becomes the resident instance -- deliberately not folded into
        `install_desktop_integration()` alone, even though that also calls
        `_ensure_stable_copy()` (see its own docstring): the latter only
        runs on the one launch that either explicitly asked for `--setup`
        or has never set up before, per `run_first_launch_setup()`'s
        one-time record, so relying on it alone would leave a *newer*
        portable download run over an already-set-up older install never
        relocating itself at all, in direct contradiction of SNX-103's own
        "running a newer version over an older install replaces it"
        acceptance criterion.
        """
        return _ensure_stable_copy()

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
                "combination Snipux can register on Windows."
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
                    "another application -- Snipux cannot use it too."
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
            return "No Snipux shortcut is currently registered."

        shortcut = self.registered_shortcut
        self.registered_shortcut = None
        if not ctypes.windll.user32.UnregisterHotKey(None, _HOTKEY_ID):
            return f"Could not release {setup_desktop.human_shortcut(shortcut)}."
        return f"Released {setup_desktop.human_shortcut(shortcut)}."

    def find_shortcut_conflict(self, shortcut: str) -> str | None:
        """None if `shortcut` looks free to register, else a short name of
        whatever already holds it -- the Windows answer (SNX-93) to
        `setup_desktop.find_shortcut_conflicts_named()`'s GNOME one, which
        Settings' conflict banner and Save button both call instead of that
        on Windows (`HotkeyEventFilter.is_available()` is what tells them
        apart, the same capability check `app.py`'s own platform-dependent
        paths already use).

        Two things can hold a combination here. The Windows Snipping Tool's
        own Win+Shift+S is invisible to any RegisterHotKey probe -- it is a
        shell feature, not a process registration -- so it is named by a
        direct comparison first. Everything else is only visible by
        actually trying to register it, since Win32 has no query for "who
        holds this" the way GNOME's introspectable schemas are: the probe
        registers `shortcut` under a throwaway id distinct from the real
        `_HOTKEY_ID` and releases it immediately, so it never actually
        holds the key -- a pure check, not a bind.

        The shortcut snipux itself already holds is never reported as a
        conflict with itself; without that check the probe above would
        find its own live registration and misreport it as a clash.
        """
        normalised = setup_desktop.normalise_shortcut(shortcut)
        if normalised is None:
            return None
        if normalised == _SNIPPING_TOOL_SHORTCUT:
            return "the Windows Snipping Tool"
        if (
            self.registered_shortcut is not None
            and setup_desktop.normalise_shortcut(self.registered_shortcut) == normalised
        ):
            return None

        translated = _accelerator_to_win32(shortcut)
        if translated is None:
            return None
        modifiers, vk = translated
        if ctypes.windll.user32.RegisterHotKey(None, _CONFLICT_PROBE_ID, modifiers, vk):
            ctypes.windll.user32.UnregisterHotKey(None, _CONFLICT_PROBE_ID)
            return None
        if ctypes.GetLastError() == _ERROR_HOTKEY_ALREADY_REGISTERED:
            return "another application"
        return None

    def default_save_folder(self) -> Path:
        raise UnimplementedPlatformError(_PLATFORM_NAME, "default_save_folder")

    def build_capture_registry(self) -> BackendRegistry:
        return capture.build_windows_registry()

    def build_recording_registry(self) -> RecorderRegistry:
        return recording.build_windows_registry()
