"""Report a problem: a prefilled GitHub issue, opened in the user's browser.

Snipux sends nothing itself. The report is put together here as a
new-issue URL and handed to the browser, where the user reads all of it
and decides whether to post it. That keeps the "no telemetry" promise in
PLATFORM-CAPABILITIES.md true: the only request snipux makes is still the
update check, and this one is made by the browser, with the user watching.

The body carries an environment block -- the facts a bug report always has
to ask for -- and, only if the user says yes, the newest part of
`crash.log` and of `snipux.log` (the failures snipux handled and noted,
see log.py). Both can hold file paths with the user's name in them, which
is why it is a question and not a default.
"""

from __future__ import annotations

import os
import platform as stdlib_platform
import sys
from pathlib import Path
from urllib.parse import quote, urlencode

from PyQt6.QtCore import PYQT_VERSION_STR, QT_VERSION_STR, QUrl
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import QMessageBox

from snipux import __version__, platform
from snipux.capture import detect_session_type

NEW_ISSUE_URL = "https://github.com/CydoEntis/snipux/issues/new"

# GitHub answers a longer new-issue URL with an error page, and some
# browsers truncate well before the 32 KB they nominally allow.
URL_BUDGET = 7000

_TITLE = "Bug: "

_TEMPLATE = (
    "### What happened\n\n\n\n"
    "### What you expected\n\n\n\n"
    "### Steps to reproduce\n\n1. \n2. \n3. \n\n"
)

_LOG_HEADING = "### crash.log (most recent part)\n\n"
_NOTES_HEADING = "### snipux.log (most recent part)\n\n"
# How much of the room the notes may take when there is a crash log too:
# the traceback is the part a report cannot do without.
_NOTES_SHARE = 3
_FENCE = "```\n"


def environment() -> str:
    """The environment block for this process, as Markdown."""
    lines = [
        f"- Snipux: {__version__}",
        f"- OS: {platform.os_name()} {_os_version()}",
    ]
    if platform.is_linux():
        desktop = os.environ.get("XDG_CURRENT_DESKTOP") or "unknown"
        lines.append(f"- Session: {detect_session_type()}, desktop: {desktop}")
    lines.append(f"- Qt: {QT_VERSION_STR}, PyQt6: {PYQT_VERSION_STR}")
    # A PyInstaller bundle (the Windows exe, the AppImage) sets `frozen`;
    # nothing tells pip, pipx and a source checkout apart, so they share one
    # answer rather than a guess.
    build = "bundled build" if getattr(sys, "frozen", False) else "Python package"
    lines.append(f"- Installed as: {build}")
    return "### Environment\n\n" + "\n".join(lines) + "\n"


def _os_version() -> str:
    # The distribution's own name ("Ubuntu 24.04.1 LTS") says more than a
    # kernel number. Elsewhere, release and build together: Windows 11
    # still reports its version as "10.0.x", and only `release()` says "11".
    try:
        return stdlib_platform.freedesktop_os_release()["PRETTY_NAME"]
    except (OSError, KeyError):
        return f"{stdlib_platform.release()} ({stdlib_platform.version()})"


def issue_url(
    env: str,
    crash_log: str | None = None,
    budget: int = URL_BUDGET,
    notes: str | None = None,
) -> str:
    """The new-issue URL: template, `env`, and as much of the end of
    `crash_log` and then of `notes` as still fits in `budget` characters
    of URL. With both, the notes get a third of the room at most."""
    body = _TEMPLATE + env
    room = budget - len(_url(body))
    notes_section = ""
    if notes:
        notes_room = room // _NOTES_SHARE if crash_log else room
        notes_section = _log_section(_NOTES_HEADING, notes, notes_room)
        room -= _encoded_len(notes_section)
    if crash_log:
        body += _log_section(_LOG_HEADING, crash_log, room)
    return _url(body + notes_section)


def _log_section(heading: str, text: str, room: int) -> str:
    """`heading` over the fenced end of `text`, or nothing when not even
    its last line fits in `room` characters of URL."""
    wrapper = "\n" + heading + _FENCE + _FENCE
    tail = log_tail(text, room - _encoded_len(wrapper))
    if not tail:
        return ""
    return "\n" + heading + _FENCE + tail + _FENCE


def log_tail(text: str, room: int) -> str:
    """The last whole lines of `text` whose URL-encoded length fits in
    `room`. Cut on a line boundary so the newest entry is never half a
    line; empty when not even the last line fits."""
    kept: list[str] = []
    used = 0
    for line in reversed(text.splitlines()):
        cost = _encoded_len(line + "\n")
        if used + cost > room:
            break
        kept.append(line + "\n")
        used += cost
    return "".join(reversed(kept))


def _encoded_len(text: str) -> int:
    return len(quote(text, safe=""))


def _url(body: str) -> str:
    return NEW_ISSUE_URL + "?" + urlencode({"title": _TITLE, "body": body}, quote_via=quote)


def read_crash_log(path: Path) -> str | None:
    """The log's text, or None when there is no log or nothing in it."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return None
    return text if text.strip() else None


def ask_to_include_log() -> bool:
    answer = QMessageBox.question(
        None,
        "Report a problem",
        "Add the most recent part of Snipux's error log to the report?\n\n"
        "It can contain file paths that include your user name. You will see "
        "the whole report in your browser before anything is posted.",
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        QMessageBox.StandardButton.No,
    )
    return answer == QMessageBox.StandardButton.Yes


def open_report(crash_log_path: Path, notes_path: Path | None = None) -> str:
    """Ask about the logs if there are any, then open the report in the
    browser. Returns the URL it opened."""
    log = read_crash_log(crash_log_path)
    notes = read_crash_log(notes_path) if notes_path is not None else None
    if (log is not None or notes is not None) and not ask_to_include_log():
        log = notes = None
    url = issue_url(environment(), log, notes=notes)
    QDesktopServices.openUrl(QUrl.fromEncoded(url.encode("ascii")))
    return url
