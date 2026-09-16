"""Hand a request to the resident snipux without loading Qt first.

Pressing the shortcut runs `snipux --snip`, a fresh process whose only job is
to tell the snipux already running to take a snip. Through `app.py` that
process imported the whole application, PyQt6 included, and built a
`QApplication` before it could send a single byte: about 160ms of a snip's
roughly 475ms, measured from the key press on an X11 GNOME desk. This module
imports nothing but the standard library, so a request to a resident that is
already up costs little more than starting Python.

It speaks the resident's own protocol and nothing more. On a POSIX system Qt's
`QLocalServer` listens on a Unix-domain socket named `SERVER_NAME` in the
directory `QDir::tempPath()` resolves to -- `$TMPDIR` when it is set, `/tmp`
otherwise -- and a request is one byte (see `app.QLocalSocketTransport`), or
that byte followed by a path to open. Where there is no such socket to reach
-- no resident yet, a stale socket file left by one that died, Windows'
named pipes -- `forward` says so, and the caller takes the full path, which
finds or becomes the resident exactly as it always has.

A lone argument that isn't `--snip`/`--settings` and doesn't look like a flag
is a path to open (`snipux shot.png`), forwarded the same way behind the
`OPEN_REQUEST_PREFIX` byte. It travels as the bytes `os.fsencode` gives it,
made absolute first -- the resident's working directory is not this
process's -- so a name that isn't valid UTF-8 on Linux, or one with spaces on
Windows, arrives unharmed.
"""

from __future__ import annotations

import os
import socket
import sys

# The resident's listening name and its request bytes. `app.QLocalSocketTransport`
# reads these rather than repeating them, so the two ends cannot drift apart.
SERVER_NAME = "snipux-resident"
SNIP_REQUEST = b"S"
SETTINGS_REQUEST = b"T"
OPEN_REQUEST_PREFIX = b"O"

_REQUESTS = {"--snip": SNIP_REQUEST, "--settings": SETTINGS_REQUEST}

# A resident that is up accepts at once; this only bounds a wedged one.
_CONNECT_TIMEOUT_S = 0.5


def socket_path(server_name: str = SERVER_NAME) -> str:
    """Where `QLocalServer` puts `server_name` on a POSIX system:
    `QDir::tempPath()`, which is `$TMPDIR` when set and `/tmp` otherwise.
    """
    return os.path.join(os.environ.get("TMPDIR") or "/tmp", server_name)


def forward(arguments: list[str], server_name: str = SERVER_NAME) -> bool:
    """Send the request `arguments` names to a running resident, and say
    whether it was delivered.

    Only a lone `--snip`, `--settings`, or a path (anything else that
    doesn't start with `-`, i.e. not a flag main() doesn't otherwise know)
    is a request; anything else belongs to the full CLI. False whenever the
    resident cannot be reached this way, which is never an error -- it is
    the caller's cue to take the full path.
    """
    if len(arguments) != 1:
        return False
    argument = arguments[0]
    if argument in _REQUESTS:
        payload = _REQUESTS[argument]
    elif not argument.startswith("-"):
        # Made absolute here, before it travels anywhere -- the resident's
        # working directory is not this process's.
        payload = OPEN_REQUEST_PREFIX + os.fsencode(os.path.abspath(argument))
    else:
        return False
    if not hasattr(socket, "AF_UNIX"):
        return False
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(_CONNECT_TIMEOUT_S)
            connection.connect(socket_path(server_name))
            connection.sendall(payload)
    except OSError:
        return False
    return True


def cli() -> int:
    """The `snipux` console script: a request is forwarded from here, and
    everything else goes through `app.cli`.
    """
    if forward(sys.argv[1:]):
        return 0
    from snipux.app import cli as full_cli

    return full_cli()


def gui() -> int:
    """The `snipuxw` GUI script: the same, through `app.gui`."""
    if forward(sys.argv[1:]):
        return 0
    from snipux.app import gui as full_gui

    return full_gui()
