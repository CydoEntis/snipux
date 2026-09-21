"""The system ffmpeg, as the Linux recorder uses it -- found, never required.

GNOME's screencast writes video only and cannot pause, so two things a
Linux recording can do rest on a system ffmpeg: joining the pieces a paused
recording is made of, and capturing the sound that goes with them from
PulseAudio (which PipeWire also serves). Where there is no ffmpeg, both are
greyed with a reason and a recording is what it always was.

`player.system_ffmpeg()` asks the same binary a different question -- can it
encode everything the export menu offers -- so it keeps its own probe. This
one sits below the views, where the recording backends can reach it
(docs/ARCHITECTURE.md: a backend must not import a view).
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass


@dataclass(frozen=True)
class Capabilities:
    """What the system ffmpeg can do for a recording."""

    binary: str
    # The `pulse` input device, which is how both the default output's
    # monitor ("what you hear") and the default microphone are captured.
    pulse_input: bool
    # Opus, the one audio codec a GNOME recording's WebM can carry that
    # Ubuntu's ffmpeg is sure to have.
    opus: bool

    @property
    def records_sound(self) -> bool:
        return self.pulse_input and self.opus


_UNPROBED = object()
_probed: "Capabilities | None | object" = _UNPROBED


def probe() -> Capabilities | None:
    """The system ffmpeg's capabilities, or None where there is none.

    Probed once per process: two ~100ms subprocesses, and the answer cannot
    change while snipux runs. Never on the path a snip opens along -- the
    first to ask is arming a recording, or its audio menu.
    """
    global _probed
    if _probed is not _UNPROBED:
        return _probed
    _probed = None
    binary = shutil.which("ffmpeg")
    if binary is None:
        return None
    try:
        devices = _listing(binary, "-devices")
        encoders = _listing(binary, "-encoders")
    except (OSError, subprocess.SubprocessError):
        # A binary that will not answer is one a recording must not rely on.
        return None
    _probed = Capabilities(
        binary=binary,
        pulse_input=_has_input_device(devices, "pulse"),
        opus=" libopus " in encoders,
    )
    return _probed


def reset() -> None:
    """Forget the probe. For tests, which need every world."""
    global _probed
    _probed = _UNPROBED


def _listing(binary: str, flag: str) -> str:
    return subprocess.run(
        [binary, "-hide_banner", flag], capture_output=True, text=True, timeout=10
    ).stdout


def _has_input_device(listing: str, name: str) -> bool:
    """Whether `-devices` lists `name` as one ffmpeg can read from.

    Each row is flags then name -- ` DE pulse  Pulse audio output` -- and
    `D` in the flags is what makes it an input.
    """
    for line in listing.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1] == name and "D" in parts[0]:
            return True
    return False
