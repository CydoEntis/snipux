"""Text recognition on Linux, through a system `tesseract` if there is one.

Linux ships no OCR engine the way Windows does, so this uses the one most
distributions package. It is found on `PATH`, never imported, never
required and never installed by snipux -- the same rules as `ffmpeg`
(CLAUDE.md). Where there is none, Hide sensitive is greyed with the
command that installs it.

The image is written to temporary files, `tesseract` reads them and prints
TSV: one row per word with its box, and the block, paragraph and line it
sits in. That is turned into the same lines of `sensitive.RecognizedWord`
the Windows recogniser returns.

**Two things are done to the picture first**, both measured on tesseract
5.5 with screen text:

- *Small images are doubled.* Screen text is 11-16 px tall, well under
  what the engine was trained on. At its own size a 13 px line read
  `sk-proj-9fK2...` as `sk-pro}-9fK2mQx7Lr4TzVbeNw3YcInG`, and missed an
  email address outright; doubled, both came back whole. Unlike Windows
  the picture is read once, not at both sizes: here the doubled read was
  never the worse one, and a second read costs as long again.
- *It is cut into horizontal bands, read at the same time.* tesseract is
  slow where Windows' engine is not: one 2560x1440 screen full of 13 px
  text took 9.5 s in one process, twice the time a snip is allowed to
  wait, and 1.9 s cut into bands on the same twenty-core machine.
  Bands overlap, so a line the cut goes through is whole in one of them,
  and each line is kept only from the band whose own share of the picture
  its middle falls in -- so nothing is returned twice and nothing cut in
  half is returned at all.

Everything that stops the picture being read -- no language data, a
tesseract that exits with an error, running out of time -- raises
`sensitive.RecognitionFailed` with the reason, exactly as on Windows, so
"nothing was checked" can never be taken for "nothing to hide". A row of
output that cannot be parsed is only skipped.

Nothing here is Linux-only code, so all of it is tested on every OS with
the process call faked.
"""

from __future__ import annotations

import math
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass

from PyQt6.QtCore import QRectF, Qt
from PyQt6.QtGui import QImage

from snipux import hostenv
from snipux.sensitive import RecognitionFailed, RecognizedWord

BINARY = "tesseract"

# The same give-up as Windows (`windows_ocr.TIMEOUT_SECONDS`), for the
# same reason: the snip is held until the read comes back, and running out
# of time is reported rather than passed off as finding nothing.
TIMEOUT_SECONDS = 5.0

# Images no longer than this on their longest edge are doubled before they
# are read. Above it the text is either already large (a HiDPI screen) or
# there is too much of it to read twice the pixels of in time.
UPSCALE_LONGEST_EDGE = 2000

# Leptonica refuses very large images; nothing on a desk gets near this.
MAX_DIMENSION = 10000

# Sent-image pixels. A band shorter than this is not worth a process of its
# own: starting tesseract and loading its model is about 0.1 s.
BAND_MIN_HEIGHT = 200
# Original-image pixels each band reaches past its own share, both ways. A
# line of text up to twice this tall is whole in the band its middle is in.
BAND_OVERLAP = 48
# More processes than this buys nothing a snip would notice.
MAX_BANDS = 16

# Sparse text: find as much text as possible in no particular order. The
# default mode looks for columns and paragraphs, which a screen full of
# toolbars, form fields and terminals does not have. Measured on a window
# holding a form and a dark terminal, this mode found one labelled value
# the default left out and never found fewer.
PAGE_SEGMENTATION_MODE = "11"

# `--list-langs` entries that are not languages: orientation detection and
# the equation detector.
_NOT_LANGUAGES = {"osd", "equ"}

# What the engine calls the language a locale names: ISO 639-1 on the
# left, tesseract's own (mostly 639-2/T) on the right. A locale not listed
# is read in English.
_LOCALE_LANGUAGES = {
    "ar": "ara", "bg": "bul", "ca": "cat", "cs": "ces", "da": "dan",
    "de": "deu", "el": "ell", "en": "eng", "es": "spa", "fi": "fin",
    "fr": "fra", "he": "heb", "hi": "hin", "hr": "hrv", "hu": "hun",
    "id": "ind", "it": "ita", "ja": "jpn", "ko": "kor", "nb": "nor",
    "nl": "nld", "no": "nor", "pl": "pol", "pt": "por", "ro": "ron",
    "ru": "rus", "sk": "slk", "sl": "slv", "sv": "swe", "th": "tha",
    "tr": "tur", "uk": "ukr", "vi": "vie",
    "zh_CN": "chi_sim", "zh_SG": "chi_sim", "zh_TW": "chi_tra", "zh_HK": "chi_tra",
}


def find_binary() -> str | None:
    """The system tesseract, or None. Its own function so tests can say
    what this machine has."""
    return shutil.which(BINARY)


def available() -> bool:
    """Whether recognition can be attempted here at all.

    Deliberately only a `PATH` lookup -- it is asked every time the overlay
    opens. A tesseract with no language data is found out when it is first
    asked to read, and said then.
    """
    return find_binary() is not None


_UNPROBED = object()
_manager: "str | None | object" = _UNPROBED


def _package_manager() -> str | None:
    """Which of the package managers we can name a command for is here.

    Looked up once per process: the greyed switch asks for its reason every
    time the overlay opens, and a distribution does not change under a
    running snipux the way an installed tesseract can.
    """
    global _manager
    if _manager is _UNPROBED:
        _manager = next(
            (name for name in ("pacman", "apt", "dnf") if shutil.which(name)), None
        )
    return _manager


def reset() -> None:
    """Forget which package manager was found. For tests."""
    global _manager
    _manager = _UNPROBED


def install_hint() -> str:
    """What to say on the greyed switch when there is no tesseract: the
    command that installs it on this distribution, when it is one we know.
    """
    return {
        "pacman": "Needs tesseract: sudo pacman -S tesseract tesseract-data-eng",
        "apt": "Needs tesseract: sudo apt install tesseract-ocr",
        "dnf": "Needs tesseract: sudo dnf install tesseract",
    }.get(_package_manager(), "Needs tesseract (with its English data) installed")


def _language_hint() -> str:
    return {
        "pacman": " (sudo pacman -S tesseract-data-eng)",
        "apt": " (sudo apt install tesseract-ocr-eng)",
        "dnf": " (sudo dnf install tesseract-langpack-eng)",
    }.get(_package_manager(), "")


@dataclass(frozen=True)
class _Band:
    """One horizontal slice of the sent image, all in sent-image pixels.

    `top`/`bottom` are the rows the slice holds; `own_top`/`own_bottom` the
    rows that are this band's alone. The own ranges tile the image with no
    gap and no overlap.
    """

    top: int
    bottom: int
    own_top: int
    own_bottom: int


def recognize(
    image: QImage,
    *,
    runner=None,
    timeout: float = TIMEOUT_SECONDS,
    processors: int | None = None,
) -> list[list[RecognizedWord]]:
    """Every word in `image`, grouped into the lines tesseract found them
    in, top to bottom.

    Rects are in `image`'s own pixel space, whatever scaling and slicing
    happened on the way to the engine.

    `runner(commands, timeout) -> [(returncode, stdout_bytes), ...]` starts
    every command at once and answers for each in order; injected so tests
    can drive this without tesseract. `processors` likewise, for how many
    bands the machine is worth.

    Raises `RecognitionFailed` when the image could not be read at all. An
    empty list means it was read and has no words in it.
    """
    if image.isNull() or image.width() == 0 or image.height() == 0:
        return []
    binary = find_binary()
    if binary is None:
        raise RecognitionFailed("tesseract is not installed")

    run = runner if runner is not None else _run
    deadline = time.monotonic() + timeout
    directory = None
    try:
        languages = _languages_to_read(_installed_languages(binary, run, timeout))
        if not languages:
            raise RecognitionFailed(
                "tesseract has no language data installed" + _language_hint()
            )

        scale = _scale_for(image.width(), image.height())
        sent = image if scale == 1.0 else image.scaled(
            max(1, round(image.width() * scale)),
            max(1, round(image.height() * scale)),
            Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        bands = _bands_for(
            sent.height(), scale, processors if processors is not None else os.cpu_count() or 1
        )
        directory = tempfile.mkdtemp(prefix="snipux-ocr-")
        commands = []
        for index, band in enumerate(bands):
            path = os.path.join(directory, f"band-{index}.bmp")
            piece = sent if len(bands) == 1 else sent.copy(
                0, band.top, sent.width(), band.bottom - band.top
            )
            # BMP rather than PNG: these files live for one call, and
            # encoding costs more than writing the pixels out.
            if not piece.save(path, "BMP"):
                raise RecognitionFailed(
                    "the snip could not be written out for text recognition"
                )
            commands.append(_command_for(binary, path, languages))
        results = run(commands, max(0.1, deadline - time.monotonic()))
    except subprocess.TimeoutExpired as exc:
        raise RecognitionFailed(
            f"text recognition took longer than {timeout:g} seconds"
        ) from exc
    except (OSError, subprocess.SubprocessError) as exc:
        raise RecognitionFailed(f"text recognition could not start ({exc})") from exc
    finally:
        if directory is not None:
            shutil.rmtree(directory, ignore_errors=True)

    lines: list[tuple[float, float, list[RecognizedWord]]] = []
    for band, (code, stdout) in zip(bands, results):
        if code != 0:
            raise RecognitionFailed(f"text recognition failed (tesseract exit code {code})")
        lines.extend(_parse(stdout, band, scale))
    lines.sort(key=lambda line: (line[0], line[1]))
    return [words for _top, _left, words in lines]


def _installed_languages(binary: str, run, timeout: float) -> list[str]:
    """What `--list-langs` names. Asked on every read rather than once: it
    takes about 10 ms, it is off the UI thread, and a language pack
    installed while snipux is running should count on the next snip."""
    [(code, stdout)] = run([[binary, "--list-langs"]], timeout)
    if code != 0:
        raise RecognitionFailed(f"text recognition failed (tesseract exit code {code})")
    names = []
    # The first line is a heading: `List of available languages in "..." (2):`
    for raw in stdout.decode("utf-8", errors="replace").splitlines()[1:]:
        name = raw.strip()
        if name and name not in _NOT_LANGUAGES:
            names.append(name)
    return names


def _languages_to_read(installed: list[str], locale: str | None = None) -> list[str]:
    """Which of the `installed` languages to read with.

    The user's own, where it is installed, and English: keys, variable
    names and most labels are English whatever the desktop speaks. Never
    everything installed -- each language added is a model to load and
    time to run, and a machine with the full set has over a hundred. With
    neither, whatever is there: reading in the wrong language still finds
    digits and most Latin text.
    """
    if locale is None:
        locale = (
            os.environ.get("LC_ALL") or os.environ.get("LC_MESSAGES") or os.environ.get("LANG") or ""
        )
    locale = locale.split(".")[0].split("@")[0]
    short = locale.split("_")[0].lower()
    chosen = []
    wanted = _LOCALE_LANGUAGES.get(locale) or _LOCALE_LANGUAGES.get(short)
    if wanted in installed:
        chosen.append(wanted)
    if "eng" in installed and "eng" not in chosen:
        chosen.append("eng")
    if not chosen and installed:
        chosen.append(sorted(installed)[0])
    return chosen


def _scale_for(width: int, height: int) -> float:
    """What to multiply an image's size by before it is read."""
    longest = max(width, height)
    if longest > MAX_DIMENSION:
        return MAX_DIMENSION / longest
    if longest <= UPSCALE_LONGEST_EDGE:
        return 2.0
    return 1.0


def _bands_for(height: int, scale: float, processors: int) -> list[_Band]:
    """Cut `height` rows of the sent image into bands, one per processor
    while each stays worth a process."""
    count = max(1, min(processors, MAX_BANDS, height // BAND_MIN_HEIGHT))
    overlap = math.ceil(BAND_OVERLAP * scale)
    bands = []
    for index in range(count):
        own_top = height * index // count
        own_bottom = height * (index + 1) // count
        bands.append(
            _Band(
                top=max(0, own_top - overlap),
                bottom=min(height, own_bottom + overlap),
                own_top=own_top,
                own_bottom=own_bottom,
            )
        )
    return bands


def _command_for(binary: str, path: str, languages: list[str]) -> list[str]:
    return [
        binary, path, "stdout",
        "-l", "+".join(languages),
        "--psm", PAGE_SEGMENTATION_MODE,
        "tsv",
    ]


def _run(commands: list[list[str]], timeout: float) -> list[tuple[int, bytes]]:
    """Start every command at once and wait for them all.

    Output goes to temporary files, not pipes: nothing here reads while the
    processes run, and a pipe nobody reads blocks its writer once it is
    full. Out of time, every process still running is killed -- a tesseract
    left behind would keep a core busy for a snip that has already been
    told no.
    """
    # snipux's own environment, minus an AppImage's bundled libraries (the
    # system tesseract must load the system's), and one thread each: the
    # bands are the parallelism, and tesseract's own threads only make
    # small images slower.
    env = hostenv.host_env()
    env["OMP_THREAD_LIMIT"] = "1"
    deadline = time.monotonic() + timeout
    started: list[tuple[subprocess.Popen, "tempfile._TemporaryFileWrapper"]] = []
    try:
        for command in commands:
            output = tempfile.TemporaryFile()
            try:
                process = subprocess.Popen(
                    command,
                    stdin=subprocess.DEVNULL,
                    stdout=output,
                    stderr=subprocess.DEVNULL,
                    env=env,
                )
            except BaseException:
                output.close()
                raise
            started.append((process, output))
        results = []
        for process, output in started:
            code = process.wait(timeout=max(0.0, deadline - time.monotonic()))
            output.seek(0)
            results.append((code, output.read()))
        return results
    finally:
        for process, output in started:
            if process.poll() is None:
                process.kill()
                process.wait()
            output.close()


def _parse(stdout: bytes, band: _Band, scale: float) -> list[tuple[float, float, list[RecognizedWord]]]:
    """The lines in one band's TSV, as (top, left, words) in the original
    image's pixels.

    A row is level, page, block, paragraph, line, word, left, top, width,
    height, confidence, text; level 5 is a word, and the four numbers
    before it say which line it belongs to. Boxes are in the band's own
    pixels: `band.top` puts them back in the sent image and `scale` back
    in the original. A row that does not parse is skipped rather than
    failing the whole result.

    A line is kept only when its middle is in the rows this band owns --
    see the module docstring.
    """
    grouped: dict[tuple[int, int, int, int], list[tuple[int, QRectF, str]]] = {}
    for raw in stdout.decode("utf-8", errors="replace").splitlines():
        parts = raw.split("\t", 11)
        if len(parts) != 12 or parts[0] != "5":
            continue
        text = parts[11].strip()
        if not text:
            continue
        try:
            key = (int(parts[1]), int(parts[2]), int(parts[3]), int(parts[4]))
            order = int(parts[5])
            left, top, width, height = (float(value) for value in parts[6:10])
        except ValueError:
            continue
        if width <= 0 or height <= 0:
            continue
        grouped.setdefault(key, []).append(
            (order, QRectF(left, top + band.top, width, height), text)
        )

    lines = []
    for key in sorted(grouped):
        words = sorted(grouped[key], key=lambda word: word[0])
        top = min(rect.top() for _order, rect, _text in words)
        bottom = max(rect.bottom() for _order, rect, _text in words)
        if not band.own_top <= (top + bottom) / 2 < band.own_bottom:
            continue
        left = min(rect.left() for _order, rect, _text in words)
        lines.append(
            (
                top / scale,
                left / scale,
                [
                    RecognizedWord(
                        text,
                        QRectF(
                            rect.x() / scale, rect.y() / scale,
                            rect.width() / scale, rect.height() / scale,
                        ),
                    )
                    for _order, rect, text in words
                ],
            )
        )
    return lines
