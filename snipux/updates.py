"""Is there a newer Snipux than this one, and can this build fetch it?

Two requests at most, both to GitHub and both public: one asking what the
newest release is, and -- only if the user then clicks Update -- one
downloading the file for this kind of build.

The check happens at most once a day, on the first launch after that day
turns over, and otherwise when someone picks it from the tray. It used to
happen only when asked, on the reasoning that a screenshot tool phoning
home unasked is one people stop trusting. That reasoning is about sending
something; this sends nothing but the request, and the alternative was a
tool that knew an update existed only if you thought to ask it. What is
still never automatic is *installing*: the daily check can only put a
sentence in the tray.

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

import dataclasses
import json
import pathlib
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

# The download is an installer, tens of megabytes of it, over whatever
# connection the user has.
_DOWNLOAD_TIMEOUT_SECONDS = 300.0

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


@dataclasses.dataclass(frozen=True)
class Release:
    """What the Releases API says about the newest release: the version,
    and every file attached to it by name.

    `assets` maps a filename to its download URL -- the platform seam picks
    which one belongs to the running build, because that is the one thing
    this module cannot know. A pip install has no file here at all.
    """

    version: str
    assets: dict[str, str] = dataclasses.field(default_factory=dict)

    def asset(self, name: str) -> str | None:
        return self.assets.get(name)


def fetch_latest_release(opener: Callable[[str], bytes] | None = None) -> Release | None:
    """The newest release, or None when the question could not be answered.

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
        body = json.loads(payload)
        tag = body["tag_name"]
    except (ValueError, KeyError, TypeError):
        return None
    if not isinstance(tag, str) or not tag.lstrip("v"):
        return None

    assets = {}
    for asset in body.get("assets") or []:
        try:
            assets[asset["name"]] = asset["browser_download_url"]
        except (KeyError, TypeError):
            continue  # one malformed entry is not a reason to know nothing
    return Release(version=tag.lstrip("v"), assets=assets)


def download(url: str, destination: pathlib.Path,
             opener: Callable[[str], bytes] | None = None) -> bool:
    """Fetch `url` into `destination`, and say whether it arrived.

    Written to a `.part` beside the target and renamed only once the whole
    body is there, so a download cut halfway through cannot leave something
    that looks like an installer and is not one.
    """
    fetch = opener if opener is not None else _read_url
    part = destination.with_suffix(destination.suffix + ".part")
    try:
        body = fetch(url)
        part.parent.mkdir(parents=True, exist_ok=True)
        part.write_bytes(body)
        part.replace(destination)
    except (OSError, urllib.error.URLError) as exc:
        print(f"Note: could not download the update: {exc}")
        part.unlink(missing_ok=True)
        return False
    return True


def _read_url(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    # No timeout as tight as the check's: this is 50 MB over whatever
    # connection the user has, and giving up on it after six seconds would
    # fail every time on a slow one.
    with urllib.request.urlopen(request, timeout=_DOWNLOAD_TIMEOUT_SECONDS) as response:
        return response.read()


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


class Download(QObject):
    """One download, off the calling thread, reporting the file it wrote.

    The same shape as `UpdateCheck` above and for the same reason: this is
    tens of megabytes over whatever connection the user has, and the tray
    menu it was started from must not sit frozen for the length of it.

    `finished` carries the path on success and None on any failure, which
    are the two outcomes `download` has.
    """

    finished = pyqtSignal(object)

    def __init__(self, url: str, destination: pathlib.Path,
                 opener: Callable[[str], bytes] | None = None, parent=None):
        super().__init__(parent)
        self._url = url
        self._destination = destination
        self._opener = opener

    def start(self) -> None:
        thread = threading.Thread(target=self._run, name="snipux-update-download")
        # Daemon, like the check's: quitting must not wait on a transfer
        # that a dead network will never finish -- and quitting is exactly
        # what happens at the end of an update.
        thread.daemon = True
        thread.start()

    def _run(self) -> None:
        ok = download(self._url, self._destination, self._opener)
        self.finished.emit(str(self._destination) if ok else None)
