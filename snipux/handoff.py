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
otherwise -- and a request is one byte (see `app.QLocalSocketTransport`).
Where there is no such socket to reach -- no resident yet, a stale socket file
left by one that died, Windows' named pipes -- `forward` says so, and the
caller takes the full path, which finds or becomes the resident exactly as it
always has.
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

    Only a lone `--snip` or `--settings` is a request; anything else belongs to
    the full CLI. False whenever the resident cannot be reached this way, which
    is never an error -- it is the caller's cue to take the full path.
    """
    if len(arguments) != 1 or arguments[0] not in _REQUESTS:
        return False
    if not hasattr(socket, "AF_UNIX"):
        return False
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(_CONNECT_TIMEOUT_S)
            connection.connect(socket_path(server_name))
            connection.sendall(_REQUESTS[arguments[0]])
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
