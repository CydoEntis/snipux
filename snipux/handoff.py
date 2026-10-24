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

`--image-editor`/`--video-editor` open that editor empty, behind a byte
each. A lone argument naming an existing image or recording is a path to open
(`snipux shot.png`), forwarded the same way behind the `OPEN_REQUEST_PREFIX`
byte; the resident decides which editor it goes to. It travels as the bytes `os.fsencode` gives it,
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
EDITOR_REQUESTS = {"image": b"I", "video": b"V"}
# A path to open in the editor its type calls for. Not `O`: a resident from
# before the editors opens every `O` path as an image, so a recording sent
# that way would fail there with a confusing message instead of not at all.
ROUTED_OPEN_PREFIX = b"P"
# What a resident sends back for the requests above that the editors added.
# One from before them ignores those without a word, and an upgrade leaves
# exactly that resident running -- the ack is how a launcher can tell.
ACK = b"K"
ACKED_REQUESTS = (*EDITOR_REQUESTS.values(), ROUTED_OPEN_PREFIX)

_EDITOR_FLAGS = {"--image-editor": "image", "--video-editor": "video"}
_REQUESTS = {
    "--snip": SNIP_REQUEST,
    "--settings": SETTINGS_REQUEST,
    **{flag: EDITOR_REQUESTS[editor] for flag, editor in _EDITOR_FLAGS.items()},
}

# A resident that is up accepts at once; this only bounds a wedged one.
_CONNECT_TIMEOUT_S = 0.5
# How long to wait for the ack. A resident from before the editors hangs up
# straight after reading, so that case is over in milliseconds whatever
# this says; only a busy resident -- one still starting up, or held by a
# capture subprocess -- ever runs this down, and it answers once its event
# loop is free.
ACK_TIMEOUT_S = 10.0
BUSY_MESSAGE = (
    "snipux: Snipux is busy and did not answer in time. If nothing opens in "
    "a moment, try again."
)


def socket_path(server_name: str = SERVER_NAME) -> str:
    """Where `QLocalServer` puts `server_name` on a POSIX system:
    `QDir::tempPath()`, which is `$TMPDIR` when set and `/tmp` otherwise.
    """
    return os.path.join(os.environ.get("TMPDIR") or "/tmp", server_name)


def listening_place(server_name: str = SERVER_NAME) -> str:
    """Where the resident listens, as a person should be told it: the
    socket's path where there is one, and the bare name where Qt uses a
    named pipe instead (Windows).
    """
    return socket_path(server_name) if hasattr(socket, "AF_UNIX") else server_name


# A copy of `tokens.EDITOR_IMAGE_SUFFIXES` + `EDITOR_RECORDING_SUFFIXES`,
# since importing those would load Qt. Only a speed-up: a file this misses
# takes the full path, which opens it all the same.
_OPENABLE_SUFFIXES = (
    ".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif",
    ".mp4", ".webm", ".mkv", ".mov",
)


def _is_quick_to_open(argument: str) -> bool:
    """Whether a path can be forwarded without the full CLI looking at it
    first: one that exists, with a suffix an editor opens. Anything else --
    a typo, a text file -- goes to `app.main`, which says what is wrong in
    the terminal it was typed into.
    """
    return (
        not argument.startswith("-")
        and os.path.splitext(argument)[1].lower() in _OPENABLE_SUFFIXES
        and os.path.isfile(argument)
    )


def forward(arguments: list[str], server_name: str = SERVER_NAME) -> bool:
    """Send the request `arguments` names to a running resident, and say
    whether it was delivered.

    Only a lone `--snip`, `--settings`, `--image-editor`,
    `--video-editor`, or the path of an existing file an editor opens
    (alone, or after one of the editor flags) is a request; anything else
    belongs to the full CLI. False whenever the
    resident cannot be reached this way, or hangs up on a request that
    expects an ack, which is never an error -- it is the caller's cue to
    take the full path. A resident that is reached but too busy to answer
    counts as delivered: it has the request.
    """
    if (
        len(arguments) == 2
        and arguments[0] in _EDITOR_FLAGS
        and not arguments[1].startswith("-")
    ):
        # `--image-editor %f` from a desktop entry, given a file: the file
        # opens in whichever editor its type calls for, as a bare path does.
        arguments = arguments[1:]
    if len(arguments) != 1:
        return False
    argument = arguments[0]
    if argument in _REQUESTS:
        payload = _REQUESTS[argument]
    elif _is_quick_to_open(argument):
        # Made absolute here, before it travels anywhere -- the resident's
        # working directory is not this process's.
        payload = ROUTED_OPEN_PREFIX + os.fsencode(os.path.abspath(argument))
    else:
        return False
    if not hasattr(socket, "AF_UNIX"):
        return False
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(_CONNECT_TIMEOUT_S)
            connection.connect(socket_path(server_name))
            connection.sendall(payload)
            if payload[:1] not in ACKED_REQUESTS:
                return True
            connection.settimeout(ACK_TIMEOUT_S)
            try:
                reply = connection.recv(len(ACK))
            except TimeoutError:
                # Busy, not old: the request is in its queue. Handing this
                # to the full CLI would send it a second time.
                print(BUSY_MESSAGE, file=sys.stderr)
                return True
            # A hang-up without the ack is a resident from before the
            # editors, and the full CLI knows what to do about one; this
            # module cannot without loading Qt.
            return reply == ACK
    except OSError:
        return False


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
