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

_TEMPLATE_PATH = Path(__file__).resolve().parent / "snipux.desktop"
_LAUNCHER_PLACEHOLDER = "Exec=__SNIPUX_LAUNCHER__"

_MEDIA_KEYS_SCHEMA = "org.gnome.settings-daemon.plugins.media-keys"
_SLOT_PATH = "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/snipux/"
_SLOT_SCHEMA = f"{_MEDIA_KEYS_SCHEMA}.custom-keybinding:{_SLOT_PATH}"

DEFAULT_SHORTCUT = "<Super><Shift>s"

# GNOME accelerator syntax: zero or more <Modifier> groups followed by a key
# name ("s", "Print", "F9"). Deliberately permissive about *which* modifiers
# and keys -- gsettings itself is the authority on what a real accelerator
# is, and hard-coding a list here would reject valid ones on layouts this
# was never tested against. What it does catch is the shapes a user actually
# mistypes: "Super+Shift+S" (the way the docs render it for humans, which
# gsettings stores happily and then never fires), an empty string, and
# anything with whitespace in it.
_SHORTCUT_RE = re.compile(r"^(<[A-Za-z]+>)*[A-Za-z0-9_]+$")

_LOGO_DIR = Path(__file__).resolve().parent / "design" / "logo"
# Matches the vendored logo/snipux-<size>.png files -- SNX-81: not
# design/logo/snipux.png (the unsized master), and not anything else that
# could later land in the same directory.
_LOGO_SIZE_RE = re.compile(r"^snipux-(\d+)\.png$")


def find_console_script() -> Path | None:
    """Locate the absolute path to *this installation's* `snipux` console
    script -- the file `pip`/`pipx` generated at install time from
    `pyproject.toml`'s `[project.scripts]` entry, not just whatever a shell
    happens to resolve "snipux" to.

    `sys.executable`'s own directory is checked first: pip (and pipx, and
    the venv `packaging/install.sh` builds) always installs a
    distribution's console scripts into the same `bin/` directory as the
    Python interpreter that runs them, so this is right regardless of venv,
    pipx, or `--user` layout. `sys.argv[0]` is deliberately not used for
    this -- bash passes through whatever the user typed as `argv[0]`
    (e.g. the bare word "snipux"), not the PATH-resolved path, so it can't
    be trusted to already be absolute. `shutil.which` is the fallback for a
    layout that guess doesn't fit.
    """
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


def human_shortcut(shortcut: str) -> str:
    """`'<Super><Shift>s'` -> `'Super+Shift+S'`, for messages.

    gsettings' angle-bracket syntax is what the binding must be *stored*
    as, but printing it back at the user is showing them the wire format --
    every doc, every settings panel and this project's own README write it
    as Super+Shift+S.
    """
    parts = re.findall(r"<([A-Za-z]+)>", shortcut)
    key = re.sub(r"<[A-Za-z]+>", "", shortcut)
    if not key:
        return shortcut
    return "+".join(parts + [key.upper() if len(key) == 1 else key])


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


def validate_shortcut(shortcut: str) -> str | None:
    """None if `shortcut` is usable, else a one-line reason it isn't.

    Worth checking rather than passing straight to gsettings, which accepts
    any string at all and simply never fires a binding it can't parse --
    a silent failure that looks exactly like the app being broken, which is
    the failure mode this whole feature exists to get away from.
    """
    if not shortcut or shortcut.strip() != shortcut or " " in shortcut:
        return "a shortcut cannot be empty or contain spaces"
    if "+" in shortcut:
        return (
            "use GNOME's accelerator syntax, not '+' -- "
            f"e.g. '<Super><Shift>x', not '{shortcut}'"
        )
    if not _SHORTCUT_RE.match(shortcut):
        return (
            f"'{shortcut}' is not a GNOME accelerator -- modifiers in angle "
            "brackets followed by a key, e.g. '<Super><Shift>x' or '<Alt>Print'"
        )
    return None


def load_shortcut(config_dir: Path | None = None) -> str:
    """The remembered shortcut, or `DEFAULT_SHORTCUT`.

    Every failure -- no file, unreadable file, malformed JSON, a value that
    would no longer validate -- falls back to the default rather than
    raising. A corrupt config must not be able to break `--setup`, which
    `install.sh` runs on every install.
    """
    path = config_path(config_dir)
    try:
        stored = json.loads(path.read_text()).get("shortcut")
    except (OSError, ValueError, AttributeError):
        return DEFAULT_SHORTCUT
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
    path = config_path(config_dir)
    try:
        document = json.loads(path.read_text())
        if not isinstance(document, dict):
            document = {}
    except (OSError, ValueError):
        document = {}
    document["shortcut"] = shortcut
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(document, indent=2) + "\n")
    except OSError:
        return False
    return True


def forget_shortcut(config_dir: Path | None = None) -> bool:
    """Delete the remembered shortcut. True if a file was removed."""
    path = config_path(config_dir)
    try:
        path.unlink()
    except OSError:
        return False
    return True


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
            ["gsettings", "set", _SLOT_SCHEMA, "binding", shortcut], check=True
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
