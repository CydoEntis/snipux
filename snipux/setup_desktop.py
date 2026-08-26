"""`snipux --setup`/`snipux --remove`: the desktop-integration steps a wheel
cannot do (or undo) on its own.

A built wheel is 37 files of importable code and nothing else -- no
`.desktop` entry, no autostart entry, no GNOME shortcut -- so `pip install
snipux` (or `pipx install snipux`) leaves a working import and no working
app (SNX-73). `packaging/install.sh` used to do all of this by hand, with a
copy of the `.desktop` file living only under `packaging/` and a second,
shell-only implementation of the GNOME custom-keybindings dance. Both now
live here instead, inside the package, so they run from an installed copy
with no repository checkout present, and `install.sh` calls `run_setup()`
(via `snipux --setup`) rather than repeating the logic -- one implementation,
not two that drift.

`run_remove()` (via `snipux --remove`) is the exact counterpart: `pipx
uninstall snipux` only removes the package, not anything `--setup` wrote
outside it, which otherwise leaves a dead autostart entry, a dead keyboard
shortcut, and a ghost application-list entry behind (SNX-83). Running
`--remove` first is what makes an uninstall actually clean.

Every step -- desktop entry, autostart entry, hicolor icon theme, GNOME
shortcut -- is attempted and reported independently, in both directions.
One failing (no `gsettings`, a read-only `~/.config`) must not stop the
rest, the same "a failure must not stop the next one" rule CLAUDE.md states
for capture backends, applied here.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from .design import PACKAGE_DIR, tokens

# PACKAGE_DIR (snipux/design/__init__.py) rather than a second
# Path(__file__)-based guess: it already knows the difference between a
# source checkout/pip install and a PyInstaller bundle (SNX-96), and this
# template ships inside the bundle the same way the design assets do.
_TEMPLATE_PATH = PACKAGE_DIR / "snipux.desktop"
_LAUNCHER_PLACEHOLDER = "Exec=__SNIPUX_LAUNCHER__"

_MEDIA_KEYS_SCHEMA = "org.gnome.settings-daemon.plugins.media-keys"
_SLOT_PATH = "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/snipux/"
_SLOT_SCHEMA = f"{_MEDIA_KEYS_SCHEMA}.custom-keybinding:{_SLOT_PATH}"

# The canonical form everywhere in snipux is the design's readable one --
# "Control+Alt+S" -- not gsettings' "<Control><Alt>s". It is what Settings
# shows, what the config file stores, and what the conflict check compares,
# so there is one spelling to reason about; `to_gsettings` converts at the
# single point that actually talks to GNOME. Modifier order is fixed at
# Control, Alt, Shift, Super, because a permutation would quietly miss a
# real clash.
DEFAULT_SHORTCUT = tokens.SHORTCUT_DEFAULT

_MODIFIER_ORDER = ("Control", "Alt", "Shift", "Super")
_GSETTINGS_MODIFIERS = {
    "Control": "<Control>",
    "Alt": "<Alt>",
    "Shift": "<Shift>",
    "Super": "<Super>",
    # gsettings also emits these spellings; accepted on the way in so a
    # binding set by GNOME Settings round-trips rather than being rejected.
    "Primary": "<Control>",
    "Meta": "<Super>",
}

# GNOME accelerator syntax: zero or more <Modifier> groups followed by a key
# name ("s", "Print", "F9"). Deliberately permissive about *which* modifiers
# and keys -- gsettings itself is the authority on what a real accelerator
# is, and hard-coding a list here would reject valid ones on layouts this
# was never tested against. What it does catch is the shapes a user actually
# mistypes: "Super+Shift+S" (the way the docs render it for humans, which
# gsettings stores happily and then never fires), an empty string, and
# anything with whitespace in it.
_SHORTCUT_RE = re.compile(r"^(<[A-Za-z]+>)*[A-Za-z0-9_]+$")

# Same reasoning as _TEMPLATE_PATH above: derived from PACKAGE_DIR so this
# resolves correctly inside a PyInstaller bundle too, not just a checkout.
_LOGO_DIR = PACKAGE_DIR / "design" / "logo"
# Matches the vendored logo/snipux-<size>.png files -- SNX-81: not
# design/logo/snipux.png (the unsized master), and not anything else that
# could later land in the same directory.
_LOGO_SIZE_RE = re.compile(r"^snipux-(\d+)\.png$")


def find_console_script() -> Path | None:
    """Locate the absolute path to *this installation's* `snipux` console
    script -- the file `pip`/`pipx` generated at install time from
    `pyproject.toml`'s `[project.scripts]` entry, not just whatever a shell
    happens to resolve "snipux" to.

    `sys.frozen` (SNX-96) is checked first: a PyInstaller bundle -- the
    Windows executable `packaging/windows/` builds -- *is* its own console
    script, with no separate pip-generated wrapper for `sys.executable`'s
    directory to hold, and the whole point of that bundle is to run on a
    machine that may have no other Python at all for `shutil.which` to find
    below. `sys.frozen` is PyInstaller's own marker for this on every
    platform it targets, not a Windows-specific check.

    Otherwise, `sys.executable`'s own directory is checked first: pip (and
    pipx, and the venv `packaging/install.sh` builds) always installs a
    distribution's console scripts into the same `bin/` directory as the
    Python interpreter that runs them, so this is right regardless of venv,
    pipx, or `--user` layout. `sys.argv[0]` is deliberately not used for
    this -- bash passes through whatever the user typed as `argv[0]`
    (e.g. the bare word "snipux"), not the PATH-resolved path, so it can't
    be trusted to already be absolute. `shutil.which` is the fallback for a
    layout that guess doesn't fit.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve()

    candidate = Path(sys.executable).resolve().parent / "snipux"
    if candidate.is_file():
        return candidate

    found = shutil.which("snipux")
    if found is not None:
        return Path(found).resolve()

    return None


def render_desktop_entry(exec_path: Path) -> str:
    """Fill in the bundled `.desktop` template's `Exec=` line with
    `exec_path`'s real, absolute path.

    A bare `Exec=snipux` would depend on `snipux` being on `PATH` in
    whatever environment GNOME builds for a graphical session -- not
    reliable, per `docs/super-shift-s-gnome.md` -- so the template ships a
    placeholder instead and this is the one place that gets replaced with
    the console script's actual location.
    """
    template = _TEMPLATE_PATH.read_text(encoding="utf-8")
    lines = [
        f"Exec={exec_path}" if line == _LAUNCHER_PLACEHOLDER else line
        for line in template.splitlines()
    ]
    return "\n".join(lines) + "\n"


def _write_entry(directory: Path, contents: str, exec_path: Path, label: str) -> bool:
    """Write `contents` as `directory/snipux.desktop`, overwriting whatever
    was there.

    The filename never changes between runs, so a second `--setup` replaces
    the first entry rather than adding a duplicate -- no extra bookkeeping
    needed to satisfy "running --setup twice leaves no duplicate ... entry".
    A permission error (e.g. a read-only `~/.config`) is caught and reported
    here rather than raised, so it doesn't take down the steps after it.
    """
    try:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "snipux.desktop"
        path.write_text(contents, encoding="utf-8")
    except OSError as exc:
        print(f"Note: could not write the {label} entry into {directory}: {exc}")
        return False
    print(f"{label.capitalize()} entry written to {path} (Exec={exec_path})")
    return True


def _remove_entry(directory: Path, label: str) -> bool:
    """Delete `directory/snipux.desktop`, the counterpart to `_write_entry()`.

    Reports plainly, rather than failing, when there is nothing to remove --
    a second `--remove`, or a first one run on a machine `--setup` never ran
    on, must not be treated as an error just because the file is already
    gone. An `OSError` while deleting (e.g. a permissions problem) is caught
    and reported here rather than raised, so it doesn't take down the steps
    after it, mirroring `_write_entry()`.
    """
    path = directory / "snipux.desktop"
    if not path.exists():
        print(f"{label.capitalize()} entry not found at {path} -- nothing to remove")
        return True
    try:
        path.unlink()
    except OSError as exc:
        print(f"Note: could not remove the {label} entry at {path}: {exc}")
        return False
    print(f"{label.capitalize()} entry removed from {path}")
    return True


def remove_icons(hicolor_dir: Path | None = None) -> bool:
    """Delete every `hicolor_dir/<size>x<size>/apps/snipux.png` that
    `install_icons()` may have written -- the counterpart step for
    `--remove`.

    Looks for whatever is actually there (`hicolor_dir/*/apps/snipux.png`)
    rather than only the sizes this installed copy of snipux currently
    vendors, so an icon left behind by an older version with a different
    size set still gets cleaned up. Each file is removed independently and a
    failure on one (e.g. a read-only `~/.local/share`) is reported and
    skipped rather than raised -- the same "one step failing must not stop
    the rest" rule CLAUDE.md states for capture backends, applied per-size
    here as `install_icons()` already does on the way in.
    """
    if hicolor_dir is None:
        hicolor_dir = (
            Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share")))
            / "icons"
            / "hicolor"
        )

    removed_sizes = []
    for target in sorted(hicolor_dir.glob("*/apps/snipux.png")):
        size = target.parent.parent.name
        try:
            target.unlink()
        except OSError as exc:
            print(f"Note: could not remove the {size} icon at {target}: {exc}")
            continue
        removed_sizes.append(size)

    if removed_sizes:
        print(f"Icon theme entries ({', '.join(removed_sizes)}) removed from {hicolor_dir}")
    else:
        print(f"Note: no icon theme entries were found under {hicolor_dir}")

    return bool(removed_sizes)


def normalise_shortcut(shortcut: str) -> str | None:
    """Any accepted spelling -> the canonical `Control+Alt+S`, or None if it
    is not a shortcut at all.

    Accepts both the readable form and gsettings' angle-bracket one, so a
    binding set through GNOME Settings, typed on the command line, or
    recorded in our own window all normalise to the same string -- which is
    what makes the conflict check able to compare them.
    """
    if not shortcut or shortcut.strip() != shortcut or " " in shortcut:
        return None

    parts = re.findall(r"<([A-Za-z]+)>", shortcut)
    remainder = re.sub(r"<[A-Za-z]+>", "", shortcut)
    if not parts:
        # Readable form: split on "+", last field is the key.
        fields = shortcut.split("+")
        parts, remainder = fields[:-1], fields[-1]

    if not remainder or not re.fullmatch(r"[A-Za-z0-9_]+", remainder):
        return None

    seen = []
    for part in parts:
        canonical = {
            "control": "Control", "primary": "Control", "ctrl": "Control",
            "alt": "Alt", "shift": "Shift", "super": "Super", "meta": "Super",
        }.get(part.lower())
        if canonical is None:
            return None
        if canonical not in seen:
            seen.append(canonical)

    key = remainder.upper() if len(remainder) == 1 else remainder
    ordered = [m for m in _MODIFIER_ORDER if m in seen]
    return "+".join(ordered + [key])


def validate_shortcut(shortcut: str) -> str | None:
    """None if `shortcut` is usable, else a one-line reason it isn't.

    Worth checking rather than passing straight to gsettings, which accepts
    any string at all and simply never fires a binding it cannot parse -- a
    silent failure indistinguishable from the app being broken, which is the
    whole reason this is configurable.
    """
    if not shortcut or shortcut.strip() != shortcut or " " in shortcut:
        return "a shortcut cannot be empty or contain spaces"
    normalised = normalise_shortcut(shortcut)
    if normalised is None:
        return (
            f"'{shortcut}' is not a shortcut -- modifiers then a key, "
            "e.g. 'Control+Alt+S' or 'Super+Shift+X'"
        )
    if "+" not in normalised:
        # A bare key would swallow that key desktop-wide.
        return f"'{shortcut}' needs at least one modifier, e.g. 'Control+Alt+S'"
    return None


def to_gsettings(shortcut: str) -> str:
    """`Control+Alt+S` -> `<Control><Alt>s`, the only form GNOME stores."""
    normalised = normalise_shortcut(shortcut) or shortcut
    *modifiers, key = normalised.split("+")
    prefix = "".join(_GSETTINGS_MODIFIERS.get(m, f"<{m}>") for m in modifiers)
    return prefix + (key.lower() if len(key) == 1 else key)


def human_shortcut(shortcut: str) -> str:
    """The readable form, for messages. Already canonical in most cases."""
    return normalise_shortcut(shortcut) or shortcut


def config_path(config_dir: Path | None = None) -> Path:
    """Where the chosen shortcut is remembered.

    The first piece of state this project persists at all -- deliberately
    one file with one key, not a settings framework. `--setup` runs on
    every `install.sh`, so without somewhere to write the choice down a
    custom shortcut would survive exactly until the next reinstall stomped
    it back to the default.
    """
    if config_dir is None:
        config_dir = Path(
            os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))
        ) / "snipux"
    return config_dir / "config.json"


def _read_config(config_dir: Path | None = None) -> dict:
    """The config document, or `{}` for any reason it can't be read.

    Every failure -- no file, unreadable file, malformed JSON, a document
    that isn't an object -- is `{}` rather than an exception. A corrupt
    config must not be able to break `--setup`, which `install.sh` runs on
    every install.
    """
    try:
        document = json.loads(config_path(config_dir).read_text())
    except (OSError, ValueError):
        return {}
    return document if isinstance(document, dict) else {}


def _write_config(key: str, value, config_dir: Path | None = None) -> bool:
    """Set one key. False (never an exception) if it can't be written -- a
    read-only config directory is a step-level note here, like every other
    step in this module.

    Reads and rewrites the whole document rather than replacing it, so one
    setting is never dropped by writing another.
    """
    document = _read_config(config_dir)
    document[key] = value
    path = config_path(config_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(document, indent=2) + "\n")
    except OSError:
        return False
    return True


def load_review_window(config_dir: Path | None = None) -> bool:
    """Whether a snip opens in a review window after capture.

    Off unless explicitly turned on: the overlay already annotates in place,
    and a window that appears after every capture is a change to the core
    flow, not a default to inherit by accident.
    """
    return _read_config(config_dir).get("review_window") is True


def save_review_window(enabled: bool, config_dir: Path | None = None) -> bool:
    return _write_config("review_window", bool(enabled), config_dir)


def version_line() -> str:
    """`snipux 0.1.0 / Qt 6.7 · X11` for the nav rail's footer.

    The session type is read, never assumed -- CLAUDE.md's rule -- and a
    missing Qt (impossible here, but this is also imported by `--setup`,
    which must not need one) degrades to the version alone.
    """
    from importlib.metadata import PackageNotFoundError, version as _version

    try:
        ours = _version("snipux")
    except PackageNotFoundError:
        ours = "dev"
    try:
        from PyQt6.QtCore import QT_VERSION_STR

        qt = f" / Qt {QT_VERSION_STR}"
    except Exception:
        qt = ""
    return f"snipux {ours}{qt} · {detect_session_type()}"


def detect_session_type() -> str:
    """`wayland`, `x11`, or `unknown` -- detected, never assumed."""
    if os.environ.get("WAYLAND_DISPLAY"):
        return "wayland"
    if os.environ.get("DISPLAY"):
        return "x11"
    return os.environ.get("XDG_SESSION_TYPE", "unknown")


def load_after_capture(config_dir: Path | None = None) -> str:
    """Which of `tokens.AFTER_CAPTURE` applies once a snip is taken.

    Defaults to the first entry (`review`)? No -- to `clip`. Opening a
    window after every capture is a change to the core flow, and an
    upgrading user who never asked for one should not suddenly get one.
    """
    stored = _read_config(config_dir).get("after_capture")
    valid = {identifier for identifier, _label, _note in tokens.AFTER_CAPTURE}
    return stored if stored in valid else "clip"


def save_after_capture(value: str, config_dir: Path | None = None) -> bool:
    return _write_config("after_capture", value, config_dir)


def load_review_window(config_dir: Path | None = None) -> bool:
    """Whether a snip opens in a review window -- the one thing `app.py`
    needs from `after_capture`, kept as its own reader so the controller
    does not have to know the vocabulary.
    """
    return load_after_capture(config_dir) == "review"


def save_review_window(enabled: bool, config_dir: Path | None = None) -> bool:
    return save_after_capture("review" if enabled else "clip", config_dir)


def load_always_copy(config_dir: Path | None = None) -> bool:
    return _read_config(config_dir).get("always_copy") is True


def save_always_copy(enabled: bool, config_dir: Path | None = None) -> bool:
    return _write_config("always_copy", bool(enabled), config_dir)


def default_save_folder() -> Path:
    return Path.home() / "Pictures" / "snipux"


def load_save_folder(config_dir: Path | None = None) -> Path:
    stored = _read_config(config_dir).get("save_folder")
    return Path(stored) if isinstance(stored, str) and stored else default_save_folder()


def save_save_folder(folder: str | Path, config_dir: Path | None = None) -> bool:
    return _write_config("save_folder", str(folder), config_dir)


def load_filename_pattern(config_dir: Path | None = None) -> str:
    stored = _read_config(config_dir).get("filename_pattern")
    return stored if isinstance(stored, str) and stored else tokens.FILENAME_DEFAULT


def save_filename_pattern(pattern: str, config_dir: Path | None = None) -> bool:
    return _write_config("filename_pattern", pattern, config_dir)


def preview_filename(folder: str | Path, pattern: str, extension: str = "png") -> str:
    """The full path a snip taken *now* would be written to.

    Rendered live under the pattern field, so the answer to "what will this
    actually produce" is visible while typing rather than after saving.
    `%c` (counter) and `%w` (active window) are not strftime's, so they are
    substituted with representative stand-ins rather than left raw.
    """
    import datetime

    stamped = datetime.datetime.now().strftime(pattern or tokens.FILENAME_DEFAULT)
    stamped = stamped.replace("%c", "1").replace("%w", "Firefox")
    return str(Path(folder) / f"{stamped}.{extension}")


def load_native_resolution(config_dir: Path | None = None) -> bool:
    return _read_config(config_dir).get("native_resolution") is True


def save_native_resolution(enabled: bool, config_dir: Path | None = None) -> bool:
    return _write_config("native_resolution", bool(enabled), config_dir)


def load_remember_tool(config_dir: Path | None = None) -> bool:
    return _read_config(config_dir).get("remember_tool") is True


def save_remember_tool(enabled: bool, config_dir: Path | None = None) -> bool:
    return _write_config("remember_tool", bool(enabled), config_dir)


def load_hints_enabled(config_dir: Path | None = None) -> bool:
    """Whether the overlay's top hint HUD (SNX-46) is shown from the start
    of a session.

    Off unless explicitly turned back on -- SNX-65 turned it off by
    default, and this is the one place that default can be reversed for
    good, rather than reaching for the overlay's own `?` escape hatch
    every single session.
    """
    return _read_config(config_dir).get("hints_enabled") is True


def save_hints_enabled(enabled: bool, config_dir: Path | None = None) -> bool:
    return _write_config("hints_enabled", bool(enabled), config_dir)


def load_tray_toggles(config_dir: Path | None = None) -> dict:
    """The four `tokens.TRAY_TOGGLES`, each falling back to its own default
    rather than to False -- three of the four ship on.
    """
    stored = _read_config(config_dir).get("tray_toggles")
    stored = stored if isinstance(stored, dict) else {}
    return {
        identifier: bool(stored.get(identifier, default))
        for identifier, _label, _note, default in tokens.TRAY_TOGGLES
    }


def save_tray_toggles(values: dict, config_dir: Path | None = None) -> bool:
    return _write_config("tray_toggles", {k: bool(v) for k, v in values.items()}, config_dir)


def load_shortcut(config_dir: Path | None = None) -> str:
    """The remembered shortcut, or `DEFAULT_SHORTCUT`.

    Every failure -- no file, unreadable file, malformed JSON, a value that
    would no longer validate -- falls back to the default rather than
    raising. A corrupt config must not be able to break `--setup`, which
    `install.sh` runs on every install.
    """
    stored = _read_config(config_dir).get("shortcut")
    if not isinstance(stored, str) or validate_shortcut(stored) is not None:
        return DEFAULT_SHORTCUT
    return stored


def save_shortcut(shortcut: str, config_dir: Path | None = None) -> bool:
    """Remember `shortcut`. False (never an exception) if it can't be
    written -- a read-only config directory is a step-level note here, the
    same as every other step in this module.

    Reads and rewrites the whole document rather than replacing it, so a
    future key added to this file isn't silently dropped by an old
    `--setup`.
    """
    return _write_config("shortcut", shortcut, config_dir)


def forget_shortcut(config_dir: Path | None = None) -> bool:
    """Delete the remembered shortcut. True if a file was removed."""
    path = config_path(config_dir)
    try:
        path.unlink()
    except OSError:
        return False
    return True


def load_setup_complete(config_dir: Path | None = None) -> bool:
    """Whether desktop integration has already run once -- either an
    explicit `--setup`, or the app's own first-launch run of it (SNX-95).

    `app.py`'s resident startup reads this before doing anything, so a
    second and every later launch skips straight past desktop integration
    rather than rewriting the same `.desktop`/autostart/icon files and
    rebinding the same shortcut every single time it starts.
    """
    return _read_config(config_dir).get("setup_complete") is True


def save_setup_complete(enabled: bool, config_dir: Path | None = None) -> bool:
    """Record whether desktop integration has run -- see
    `load_setup_complete`. Stored in the same `config.json` `forget_shortcut`
    (and so `run_remove`) already deletes, which is what makes `--remove`
    clearing this record, so a later launch sets up again, fall out for
    free rather than needing its own removal step.
    """
    return _write_config("setup_complete", bool(enabled), config_dir)


# Schemas GNOME keeps its own keyboard shortcuts in. Not exhaustive by
# design -- these are the ones that hold the bindings a user is realistically
# about to collide with, and an unknown schema on some other desktop is a
# missing warning, not a crash.
_BINDING_SCHEMAS = (
    "org.gnome.shell.keybindings",
    "org.gnome.desktop.wm.keybindings",
    "org.gnome.settings-daemon.plugins.media-keys",
    "org.gnome.mutter.keybindings",
)

# `gsettings list-recursively` prints "<schema> <key> <value>", where value is
# either a GVariant array of accelerators (['<Super>n'], @as []) or a bare
# quoted string. One line, three fields, the rest is the value.
_SETTING_LINE_RE = re.compile(r"^(\S+)\s+(\S+)\s+(.*)$")


def find_shortcut_conflicts(shortcut: str) -> list[tuple[str, str]]:
    """Every GNOME setting already bound to `shortcut`, as (schema, key).

    This is the check that would have saved the trouble that prompted the
    whole feature: GNOME accepts a duplicate binding without a word and
    then fires whichever owner it likes, which from the losing app's side
    is indistinguishable from being broken.

    snipux's own slot is excluded -- rebinding to what is already bound is
    not a conflict.

    Blind to anything that is not a GNOME setting. An application that
    grabs a key directly (many do) owns it just as effectively and cannot
    be seen from here, so an empty list means "nothing in GNOME claims
    this", never "this key is definitely free". Returns empty rather than
    raising on any failure: no gsettings, an unreadable schema, output in
    an unexpected shape.
    """
    if shutil.which("gsettings") is None:
        return []

    conflicts: list[tuple[str, str]] = []
    for schema in _BINDING_SCHEMAS:
        try:
            output = subprocess.run(
                ["gsettings", "list-recursively", schema],
                capture_output=True,
                text=True,
                check=True,
            ).stdout
        except (OSError, subprocess.CalledProcessError):
            continue
        for line in output.splitlines():
            match = _SETTING_LINE_RE.match(line.strip())
            if match is None:
                continue
            found_schema, key, value = match.groups()
            # Quoted whole-word match: '<Super>n' must not be found inside
            # '<Super><Shift>n', and 'Print' must not match 'Print_Screen'.
            if f"'{shortcut}'" in value:
                conflicts.append((found_schema, key))
    return conflicts


# Human names for the GNOME settings people actually collide with. A key
# absent from here still reports as a conflict -- it just names the schema
# key instead of a sentence, which is worse copy but never a missed clash.
_CONFLICT_NAMES = {
    "screenshot": "GNOME's \u201cTake a screenshot\u201d",
    "area-screenshot": "GNOME's \u201cScreenshot of an area\u201d",
    "window-screenshot": "GNOME's \u201cScreenshot of a window\u201d",
    "terminal": "GNOME's \u201cLaunch terminal\u201d",
    "screensaver": "GNOME's \u201cLock screen\u201d",
    "logout": "GNOME's \u201cLog out\u201d",
    "toggle-overview": "GNOME's \u201cActivities overview\u201d",
    "switch-to-workspace-left": "GNOME's \u201cWorkspace left\u201d",
    "switch-to-workspace-right": "GNOME's \u201cWorkspace right\u201d",
    "close": "GNOME's \u201cClose window\u201d",
}


def find_shortcut_conflicts_named(shortcut: str) -> list[tuple[str, str]]:
    """Every GNOME setting already bound to `shortcut`, as (key, owner name).

    The check that would have saved the trouble this whole feature exists
    for: GNOME accepts a duplicate binding without a word and then fires
    whichever owner it likes, which from the losing application's side is
    indistinguishable from being broken.

    Compares *normalised* forms, so a binding GNOME stores as
    `<Primary><Alt>t` is recognised as the same thing the user just recorded
    as `Control+Alt+T`. snipux's own slot is excluded -- rebinding to what
    is already bound is not a conflict.

    Blind to anything that is not a GNOME setting: an application that grabs
    a key directly owns it just as effectively and cannot be seen from here,
    which is why the window never claims a key is *free*.
    """
    wanted = normalise_shortcut(shortcut)
    if wanted is None or shutil.which("gsettings") is None:
        return []

    conflicts: list[tuple[str, str]] = []
    for schema in _BINDING_SCHEMAS:
        try:
            output = subprocess.run(
                ["gsettings", "list-recursively", schema],
                capture_output=True,
                text=True,
                check=True,
            ).stdout
        except (OSError, subprocess.CalledProcessError):
            continue
        for line in output.splitlines():
            match = _SETTING_LINE_RE.match(line.strip())
            if match is None:
                continue
            _found_schema, key, value = match.groups()
            for candidate in re.findall(r"'([^']+)'", value):
                if normalise_shortcut(candidate) != wanted:
                    continue
                name = _CONFLICT_NAMES.get(key)
                conflicts.append((key, name or f"bound to {key.replace('-', ' ')}"))
                break

    # The custom-keybindings slots, which list-recursively above does not
    # reach: they live at paths, not in a fixed schema.
    for path in _custom_keybinding_paths():
        if path == _SLOT_PATH:
            continue
        binding = _custom_keybinding_value(path, "binding")
        if binding and normalise_shortcut(binding) == wanted:
            label = _custom_keybinding_value(path, "name") or "another custom shortcut"
            conflicts.append((path, f"bound to \u201c{label}\u201d"))
    return conflicts


def _custom_keybinding_paths() -> list[str]:
    try:
        raw = subprocess.run(
            ["gsettings", "get", _MEDIA_KEYS_SCHEMA, "custom-keybindings"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return []
    return re.findall(r"'([^']+)'", raw)


def _custom_keybinding_value(path: str, key: str) -> str:
    try:
        raw = subprocess.run(
            ["gsettings", "get", f"{_MEDIA_KEYS_SCHEMA}.custom-keybinding:{path}", key],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return ""
    return raw.strip("'")


def describe_conflicts(conflicts: list[tuple[str, str]]) -> str:
    """One human sentence for `find_shortcut_conflicts`' result."""
    if not conflicts:
        return ""
    names = ", ".join(key.replace("-", " ") for _, key in conflicts[:3])
    if len(conflicts) > 3:
        names += f", and {len(conflicts) - 3} more"
    return f"Already used by GNOME for: {names}"


def _append_slot(current_keybindings: str) -> str:
    """Splice `_SLOT_PATH` into gsettings' `custom-keybindings` list value,
    keeping whatever was already there.

    `current_keybindings` is a GVariant array-of-strings rendered as text,
    e.g. `"@as []"` (empty) or `"['/path/a/', '/path/b/']"`. Reusing the
    existing slot (rather than adding a second one) when it's already
    present is what makes running `--setup` twice safe; splicing rather
    than replacing is what keeps a shortcut the user bound by hand.
    """
    if f"'{_SLOT_PATH}'" in current_keybindings:
        return current_keybindings
    if current_keybindings == "@as []":
        return f"['{_SLOT_PATH}']"
    return current_keybindings.rstrip().rstrip("]") + f", '{_SLOT_PATH}']"


def bind_gnome_shortcut(exec_path: Path, shortcut: str | None = None) -> str:
    """Bind `shortcut` to `exec_path --snip` via GNOME's
    custom-keybindings mechanism, the same slot `packaging/install.sh` used
    to set up by hand -- see `docs/super-shift-s-gnome.md` for what this
    does and how to undo it.

    `shortcut` defaults to whatever `load_shortcut` remembers, which is
    what keeps a user's own choice from being stomped back to
    `DEFAULT_SHORTCUT` by the `--setup` that every `install.sh` run
    performs.

    Returns a one-line, human-readable report of what happened. Never
    raises: no `gsettings` (not GNOME) or the schema being unreadable is a
    normal, expected case here, not an error -- the acceptance criterion is
    that a step which can't be done is reported plainly, not that the whole
    command fails because of it.
    """
    if shortcut is None:
        shortcut = load_shortcut()

    if shutil.which("gsettings") is None:
        return (
            f"Note: gsettings not found -- cannot bind {human_shortcut(shortcut)} "
            "automatically. See docs/super-shift-s-gnome.md to bind it by hand."
        )

    try:
        current = subprocess.run(
            ["gsettings", "get", _MEDIA_KEYS_SCHEMA, "custom-keybindings"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return (
            "Note: could not read GNOME's custom-keybindings list -- cannot "
            f"bind {human_shortcut(shortcut)} automatically. See "
            "docs/super-shift-s-gnome.md to bind it by hand."
        )

    new_keybindings = _append_slot(current)
    launcher_cmd = f"{exec_path} --snip"

    try:
        subprocess.run(
            ["gsettings", "set", _MEDIA_KEYS_SCHEMA, "custom-keybindings", new_keybindings],
            check=True,
        )
        subprocess.run(["gsettings", "set", _SLOT_SCHEMA, "name", "snipux"], check=True)
        subprocess.run(["gsettings", "set", _SLOT_SCHEMA, "command", launcher_cmd], check=True)
        subprocess.run(
            ["gsettings", "set", _SLOT_SCHEMA, "binding", to_gsettings(shortcut)],
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return (
            "Note: setting the GNOME shortcut failed -- cannot bind "
            f"{human_shortcut(shortcut)} automatically. See docs/super-shift-s-gnome.md "
            "to bind it by hand."
        )

    return f"Bound {human_shortcut(shortcut)} to run: {launcher_cmd}"


def _remove_slot(current_keybindings: str) -> str:
    """The inverse of `_append_slot()`: splice `_SLOT_PATH` out of
    gsettings' `custom-keybindings` list value, keeping every other entry
    exactly as it was and in the same order.

    Picks the entry out by content (not by string position) so it works
    whether the slot is first, last, or in the middle of the list -- the
    same "splice, don't replace" care `_append_slot()` already takes on the
    way in, per the ticket, is what keeps a shortcut the user bound by hand
    intact here.
    """
    if f"'{_SLOT_PATH}'" not in current_keybindings:
        return current_keybindings
    remaining = [
        path for path in re.findall(r"'([^']*)'", current_keybindings) if path != _SLOT_PATH
    ]
    if not remaining:
        return "@as []"
    return "[" + ", ".join(f"'{path}'" for path in remaining) + "]"


def unbind_gnome_shortcut() -> str:
    """Remove the Super+Shift+S GNOME custom-keybinding slot
    `bind_gnome_shortcut()` set up -- see `docs/super-shift-s-gnome.md` for
    what this undoes.

    Splices `_SLOT_PATH` out of the `custom-keybindings` list (via
    `_remove_slot()`) rather than resetting the whole list, so a shortcut
    the user bound by hand independently survives -- the same care
    `bind_gnome_shortcut()` takes on the way in, per the ticket. Also resets
    the slot's own schema (name/command/binding) so nothing about our entry
    lingers in dconf once it's no longer listed.

    Returns a one-line, human-readable report of what happened. Never
    raises: no `gsettings` (not GNOME), the schema being unreadable, or the
    shortcut simply never having been set are all normal, expected cases
    here, not errors -- the acceptance criterion is that a step which can't
    be done (or has nothing to do) is reported plainly, not that the whole
    command fails because of it.
    """
    if shutil.which("gsettings") is None:
        return (
            "Note: gsettings not found -- nothing to remove for the "
            "Super+Shift+S shortcut."
        )

    try:
        current = subprocess.run(
            ["gsettings", "get", _MEDIA_KEYS_SCHEMA, "custom-keybindings"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return (
            "Note: could not read GNOME's custom-keybindings list -- cannot "
            "remove the Super+Shift+S shortcut automatically. See "
            "docs/super-shift-s-gnome.md to remove it by hand."
        )

    if f"'{_SLOT_PATH}'" not in current:
        return "Super+Shift+S shortcut was not set -- nothing to remove."

    new_keybindings = _remove_slot(current)

    try:
        subprocess.run(
            ["gsettings", "set", _MEDIA_KEYS_SCHEMA, "custom-keybindings", new_keybindings],
            check=True,
        )
        subprocess.run(["gsettings", "reset-recursively", _SLOT_SCHEMA], check=True)
    except (OSError, subprocess.CalledProcessError):
        return (
            "Note: removing the GNOME shortcut failed. See "
            "docs/super-shift-s-gnome.md to remove it by hand."
        )

    return "Removed the Super+Shift+S shortcut."


def install_icons(hicolor_dir: Path | None = None) -> bool:
    """Copy each vendored `design/logo/snipux-<size>.png` into the user's
    hicolor icon theme, at `hicolor_dir/<size>x<size>/apps/snipux.png` --
    the layout GNOME's app list and the window switcher actually search
    when they resolve the desktop entry's `Icon=snipux` (SNX-81), rather
    than a literal path the theme lookup never looks at.

    Returns whether at least one size was installed, so `run_setup()` can
    print an accurate report the same way its other steps do. Each size is
    attempted independently and a failure on one (e.g. a read-only
    `~/.local/share`) is reported and skipped rather than raised -- the
    same "one step failing must not stop the rest" rule CLAUDE.md states
    for capture backends, applied per-size here so one unwritable
    directory doesn't also take out the sizes after it.
    """
    if hicolor_dir is None:
        hicolor_dir = (
            Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share")))
            / "icons"
            / "hicolor"
        )

    installed_sizes = []
    for path in sorted(_LOGO_DIR.glob("snipux-*.png")):
        match = _LOGO_SIZE_RE.match(path.name)
        if match is None:
            continue
        size = match.group(1)
        target_dir = hicolor_dir / f"{size}x{size}" / "apps"
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target_dir / "snipux.png")
        except OSError as exc:
            print(f"Note: could not install the {size}x{size} icon into {target_dir}: {exc}")
            continue
        installed_sizes.append(size)

    if installed_sizes:
        print(f"Icon theme entries ({', '.join(installed_sizes)}px) written under {hicolor_dir}")
    else:
        print(f"Note: no icon theme entries were written under {hicolor_dir}")

    return bool(installed_sizes)


def render_ico() -> bytes | None:
    """Pack the vendored `design/logo/snipux-<size>.png` files into a
    single multi-resolution Windows `.ico` -- what `platform/windows.py`
    writes out for its Start Menu and Startup shortcuts to point their icon
    at (SNX-92), the Windows analogue of `install_icons()` copying the same
    PNGs into the hicolor theme on Linux.

    Wraps each PNG's own bytes into an ICO container rather than decoding
    and re-encoding them: Windows' `.ico` format has accepted PNG-compressed
    entries (instead of raw BMP ones) since Vista, so this is just building
    an `ICONDIR`/`ICONDIRENTRY` table (the stdlib `struct` module, same as
    `capture.py`'s hand-rolled `_BitmapInfoHeader`/`_RECT`) around bytes
    already on disk -- no pixel decoding, and so no reason to add an
    imaging dependency (Pillow, numpy) CLAUDE.md already rules out for a
    screenshot tool just to concatenate bytes.

    Capped at 256px: `ICONDIRENTRY`'s width/height fields are one byte each
    (0 meaning 256), so the vendored 512px master cannot be represented
    there and is left out -- every other vendored size fits.

    Returns None if no vendored PNG is usable at all, the same "nothing to
    install" shape `install_icons()` reports for the same directory, so a
    caller can tell "built a real icon" from "built nothing" without
    inspecting the bytes.
    """
    import struct

    sized = []
    for path in sorted(_LOGO_DIR.glob("snipux-*.png")):
        match = _LOGO_SIZE_RE.match(path.name)
        if match is None:
            continue
        size = int(match.group(1))
        if size > 256:
            continue
        try:
            sized.append((size, path.read_bytes()))
        except OSError:
            continue

    if not sized:
        return None
    sized.sort()

    header = struct.pack("<HHH", 0, 1, len(sized))
    directory = b""
    image_data = b""
    offset = len(header) + 16 * len(sized)  # each ICONDIRENTRY is 16 bytes
    for size, data in sized:
        edge = size if size < 256 else 0  # 0 means 256, per the format
        # BBBB: width, height, palette colours (0 = >=8bpp), reserved.
        # HH: colour planes (1), bits per pixel (32, RGBA).
        # II: byte count, offset from the start of the file.
        directory += struct.pack("<BBBBHHII", edge, edge, 0, 0, 1, 32, len(data), offset)
        image_data += data
        offset += len(data)

    return header + directory + image_data


def run_setup(
    *,
    exec_path: Path | None = None,
    applications_dir: Path | None = None,
    autostart_dir: Path | None = None,
    hicolor_dir: Path | None = None,
    config_dir: Path | None = None,
    shortcut: str | None = None,
) -> int:
    """The body of `snipux --setup`: desktop entry, autostart entry, GNOME
    shortcut -- everything `packaging/install.sh` used to do by hand after
    building its venv, now runnable straight from an installed copy of
    snipux with no repository checkout present.

    `exec_path`/`applications_dir`/`autostart_dir`/`hicolor_dir` default to
    the real console script and XDG locations, and are only ever
    overridden by tests -- the same None-means-"build the real thing"
    pattern `snipux/app.py` already uses for `registry`/`transport`.

    Returns 1 (and prints why, to stderr) only when the console script
    itself can't be found -- every other step below can produce a correct
    `Exec=` line without it, so nothing else here is worth attempting.
    Every other failure is a step-level note, not a nonzero exit: no
    `gsettings`, or a read-only config directory, still leaves the rest of
    setup usable, and install.sh must not abort the install because of it.
    """
    if exec_path is None:
        exec_path = find_console_script()
    if exec_path is None:
        print(
            "error: could not locate the installed snipux console script -- "
            "is snipux actually installed (pip install / pipx install), "
            "rather than just being run from a checkout?",
            file=sys.stderr,
        )
        return 1

    if applications_dir is None:
        applications_dir = (
            Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share")))
            / "applications"
        )
    if autostart_dir is None:
        autostart_dir = (
            Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
            / "autostart"
        )

    # A shortcut given on the command line is validated *before* anything
    # is written: it is the one argument a user types by hand here, and
    # failing on it after half the setup has run would leave the install in
    # a state neither they nor `install.sh` asked for. Unlike every other
    # failure in this function, a bad shortcut is worth a nonzero exit --
    # the user asked for something specific and did not get it.
    if shortcut is not None:
        problem = validate_shortcut(shortcut)
        if problem is not None:
            print(f"error: {problem}", file=sys.stderr)
            return 1
        if not save_shortcut(shortcut, config_dir):
            print(
                f"Note: could not write {config_path(config_dir)} -- "
                f"{human_shortcut(shortcut)} will be bound now, but the next --setup "
                "(which every install.sh run performs) will revert it.",
                file=sys.stderr,
            )

    contents = render_desktop_entry(exec_path)

    _write_entry(applications_dir, contents, exec_path, "desktop")
    _write_entry(autostart_dir, contents, exec_path, "autostart")
    install_icons(hicolor_dir)
    print(bind_gnome_shortcut(exec_path, load_shortcut(config_dir)))

    return 0


def run_remove(
    *,
    applications_dir: Path | None = None,
    autostart_dir: Path | None = None,
    hicolor_dir: Path | None = None,
    config_dir: Path | None = None,
) -> int:
    """The body of `snipux --remove`: the exact counterpart to
    `run_setup()` -- deletes the desktop entry, the autostart entry, the
    installed icons, and the GNOME Super+Shift+S shortcut slot, so
    `pipx uninstall snipux` afterwards leaves nothing behind (SNX-83).

    `applications_dir`/`autostart_dir`/`hicolor_dir` default to the same
    real XDG locations `run_setup()` uses, and are only ever overridden by
    tests.

    Unlike `run_setup()`, there is no console script to locate first: every
    step here only deletes things that may or may not already be there, so
    nothing depends on where (or whether) `snipux` itself is still
    installed. Always returns 0 -- every step below already reports its own
    failure or absence as a note rather than raising, the same "one step
    failing must not stop the rest" rule CLAUDE.md states for capture
    backends, applied here so running `--remove` right before an uninstall
    can't itself fail the uninstall.

    `forget_shortcut()` below deletes the whole `config.json`, not just the
    shortcut key -- which is also where `load_setup_complete()` records
    that desktop integration has already run (SNX-95), so that record goes
    away with it. That is what makes the next launch run first-launch setup
    again rather than wrongly assuming this install is still set up.
    """
    if applications_dir is None:
        applications_dir = (
            Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share")))
            / "applications"
        )
    if autostart_dir is None:
        autostart_dir = (
            Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
            / "autostart"
        )

    _remove_entry(applications_dir, "desktop")
    _remove_entry(autostart_dir, "autostart")
    remove_icons(hicolor_dir)
    print(unbind_gnome_shortcut())
    # The remembered shortcut is something --setup wrote outside the
    # package too, so leaving it behind would be the same "dead leftover"
    # SNX-83 exists to prevent.
    if forget_shortcut(config_dir):
        print(f"Removed {config_path(config_dir)}.")

    return 0
