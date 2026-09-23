"""Is there a newer Snipux than this one?

Nothing here runs on its own. There is no timer, no check at startup and
no traffic of any kind until someone picks "Check for updates" from the
tray -- a screenshot tool that phones home unasked is a screenshot tool
people stop trusting, and the only thing this would buy by doing it
automatically is telling someone about a release a few days sooner.

The whole check is one GET to the GitHub Releases API, which is public and
needs no token. `urllib` rather than anything new: CLAUDE.md's dependency
list is PyQt6, jeepney and pytest, and one HTTPS request is not worth a
fourth.

Every failure returns rather than raises -- a network that is down, a
proxy that refuses, a rate limit, a JSON shape that changed. Not knowing
whether there is an update is a normal outcome of asking, not an error the
user did anything to cause.
"""

from __future__ import annotations

import json
import re
import threading
import urllib.error
import urllib.request
from typing import Callable

from PyQt6.QtCore import QObject, pyqtSignal

LATEST_RELEASE_URL = "https://api.github.com/repos/CydoEntis/snipux/releases/latest"

# GitHub rejects requests with no User-Agent outright.
_USER_AGENT = "snipux-update-check"

# Long enough for a slow connection, short enough that a wedged proxy does
# not leave a thread sitting there for the rest of the session.
_TIMEOUT_SECONDS = 6.0

_VERSION_RE = re.compile(r"^v?(\d+(?:\.\d+)*)")


def parse_version(text: str) -> tuple[int, ...] | None:
    """`"v1.0.2"` -> `(1, 0, 2)`, or None for anything that is not a
    plain dotted version.

    Anything after the numbers -- `1.1.0-rc1`, `1.1.0+build` -- is ignored
    rather than parsed: this only has to answer "is that bigger than mine",
    and a pre-release that compares equal to its final version is a better
    answer than a crash.
    """
    match = _VERSION_RE.match(text.strip())
    if match is None:
        return None
    return tuple(int(part) for part in match.group(1).split("."))


def is_newer(candidate: str, current: str) -> bool:
    """Whether `candidate` is a later version than `current`.

    False when either is unparseable: an answer nobody can read is not
    grounds for telling someone to go and download something.
    """
    new, mine = parse_version(candidate), parse_version(current)
    if new is None or mine is None:
        return False
    # Zero-padded so 1.1 and 1.1.0 compare equal rather than by length.
    length = max(len(new), len(mine))
    return new + (0,) * (length - len(new)) > mine + (0,) * (length - len(mine))


def fetch_latest_version(opener: Callable[[str], bytes] | None = None) -> str | None:
    """The newest released version, as GitHub reports it, or None.

    `opener` is the network call, injected so a test can answer without
    reaching the internet -- the suite must never depend on a release
    existing, on GitHub being up, or on this machine having a connection.
    """
    fetch = opener if opener is not None else _read_latest_release
    try:
        payload = fetch(LATEST_RELEASE_URL)
    except (OSError, urllib.error.URLError):
        return None

    try:
        tag = json.loads(payload)["tag_name"]
    except (ValueError, KeyError, TypeError):
        return None
    if not isinstance(tag, str):
        return None
    return tag.lstrip("v") or None


def _read_latest_release(url: str) -> bytes:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": _USER_AGENT, "Accept": "application/vnd.github+json"},
    )
    with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as response:
        return response.read()


class UpdateCheck(QObject):
    """One check, off the UI thread, reporting back through `finished`.

    A plain `threading.Thread` rather than a QThread: there is no Qt object
    living in that thread and nothing to keep alive after the one call, so
    the only thing needed from Qt is the signal, which marshals the result
    back to the thread this object lives in.

    `finished` carries the newest version, or None when the check could not
    be made -- the same two outcomes `fetch_latest_version` has.
    """

    finished = pyqtSignal(object)

    def __init__(self, opener: Callable[[str], bytes] | None = None, parent=None):
        super().__init__(parent)
        self._opener = opener

    def start(self) -> None:
        thread = threading.Thread(target=self._run, name="snipux-update-check")
        # Daemon: quitting Snipux must not wait on a request that a dead
        # network will not answer for another few seconds.
        thread.daemon = True
        thread.start()

    def _run(self) -> None:
        self.finished.emit(fetch_latest_version(self._opener))
