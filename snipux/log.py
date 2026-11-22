"""Notes: things that went wrong and were handled, written where they can
be found afterwards.

These used to be bare `print()`s. The windowless Windows build and a tray
app started at login have no console, so every one of them -- a shortcut
that could not be written, a snip that failed with no tray to say so --
went nowhere, and a bug report had only `crash.log`, which holds unhandled
exceptions and nothing else.

`note()` still prints, so a terminal shows what it always did, and also
appends the line to `snipux.log` beside `crash.log`. Only failures and
warnings belong here. What a command prints as its answer (`--setup`
saying what it wrote, `--list-backends`) stays a plain `print()`.
"""

from __future__ import annotations

import datetime
import sys
from pathlib import Path

from . import __version__

# Cut back to its newest half past this, the same as crash.log: small
# enough to attach to a bug report however long the process ran.
NOTES_LIMIT_BYTES = 256 * 1024


def notes_path() -> Path:
    """`snipux.log`, in the folder `config.json` and `crash.log` live in."""
    # Imported here because setup_desktop reports through this module.
    from . import setup_desktop

    return setup_desktop.config_path().parent / "snipux.log"


def note(message: str, *, to_stderr: bool = False, path: Path | None = None) -> None:
    """Print `message` as before and keep a dated copy in the notes file.

    Never raises: this is called from the handlers of other failures, and
    a log that cannot be written must not become a second one.
    """
    stream = sys.stderr if to_stderr else sys.stdout
    # None under pythonw and the windowless launcher: no console to write to.
    if stream is not None:
        try:
            print(message, file=stream)
        except (OSError, ValueError):
            pass  # a closed or broken console; the file below still gets it
    try:
        target = path if path is not None else notes_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        _trim(target)
        stamp = datetime.datetime.now().isoformat(timespec="seconds")
        with target.open("a", encoding="utf-8") as notes:
            notes.write(f"{stamp} snipux {__version__}: {message}\n")
    except OSError:
        pass  # nowhere to keep it; the console, if any, already has it


def _trim(path: Path) -> None:
    """Cut `path` back to its newest half once it passes the limit, from the
    start of a line."""
    try:
        if path.stat().st_size <= NOTES_LIMIT_BYTES:
            return
    except FileNotFoundError:
        return
    tail = path.read_bytes()[-(NOTES_LIMIT_BYTES // 2):]
    boundary = tail.find(b"\n")
    path.write_bytes(tail[boundary + 1:])
