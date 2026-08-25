"""`snipux --setup`: the desktop-integration steps a wheel cannot do on its
own.

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

Every step -- desktop entry, autostart entry, GNOME shortcut -- is attempted
and reported independently. One failing (no `gsettings`, a read-only
`~/.config`) must not stop the rest, the same "a failure must not stop the
next one" rule CLAUDE.md states for capture backends, applied here.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

_TEMPLATE_PATH = Path(__file__).resolve().parent / "snipux.desktop"
_LAUNCHER_PLACEHOLDER = "Exec=__SNIPUX_LAUNCHER__"

_MEDIA_KEYS_SCHEMA = "org.gnome.settings-daemon.plugins.media-keys"
_SLOT_PATH = "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/snipux/"
_SLOT_SCHEMA = f"{_MEDIA_KEYS_SCHEMA}.custom-keybinding:{_SLOT_PATH}"


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


def bind_gnome_shortcut(exec_path: Path) -> str:
    """Bind Super+Shift+S to `exec_path --snip` via GNOME's
    custom-keybindings mechanism, the same slot `packaging/install.sh` used
    to set up by hand -- see `docs/super-shift-s-gnome.md` for what this
    does and how to undo it.

    Returns a one-line, human-readable report of what happened. Never
    raises: no `gsettings` (not GNOME) or the schema being unreadable is a
    normal, expected case here, not an error -- the acceptance criterion is
    that a step which can't be done is reported plainly, not that the whole
    command fails because of it.
    """
    if shutil.which("gsettings") is None:
        return (
            "Note: gsettings not found -- cannot bind Super+Shift+S "
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
            "bind Super+Shift+S automatically. See "
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
            ["gsettings", "set", _SLOT_SCHEMA, "binding", "<Super><Shift>s"], check=True
        )
    except (OSError, subprocess.CalledProcessError):
        return (
            "Note: setting the GNOME shortcut failed -- cannot bind "
            "Super+Shift+S automatically. See docs/super-shift-s-gnome.md "
            "to bind it by hand."
        )

    return f"Bound Super+Shift+S to run: {launcher_cmd}"


def run_setup(
    *,
    exec_path: Path | None = None,
    applications_dir: Path | None = None,
    autostart_dir: Path | None = None,
) -> int:
    """The body of `snipux --setup`: desktop entry, autostart entry, GNOME
    shortcut -- everything `packaging/install.sh` used to do by hand after
    building its venv, now runnable straight from an installed copy of
    snipux with no repository checkout present.

    `exec_path`/`applications_dir`/`autostart_dir` default to the real
    console script and XDG locations, and are only ever overridden by
    tests -- the same None-means-"build the real thing" pattern
    `snipux/app.py` already uses for `registry`/`transport`.

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

    contents = render_desktop_entry(exec_path)

    _write_entry(applications_dir, contents, exec_path, "desktop")
    _write_entry(autostart_dir, contents, exec_path, "autostart")
    print(bind_gnome_shortcut(exec_path))

    return 0
