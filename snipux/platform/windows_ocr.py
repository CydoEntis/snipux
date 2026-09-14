"""Text recognition through the OCR engine built into Windows 10 and 11.

`Windows.Media.Ocr` ships with the OS, so this adds no dependency -- but it
is a WinRT API, and Python has no way to call WinRT without one. PowerShell
5.1 can, and is on every supported Windows, so recognition runs there: the
image is written to a temporary file, a short script reads it back through
WinRT and prints one line per word.

Measured on Windows 11: about 0.3 s per call for a 1600x900 selection, of
which OCR itself is under 30 ms and the rest is PowerShell starting.

**Small images are read twice**, at their own size and doubled, in the same
PowerShell process. Neither size is reliably better. Screen text at 1x is
11-16 px tall, where the engine misreads characters -- doubling fixed
`gmall.com` back to `gmail.com`. But on a real dark-theme screenshot the
doubled image lost `KEY=api-123...` outright while the original read it. A
value either reading finds is a value to hide, and since PowerShell's start
dominates the cost, the second read is nearly free. The same value found
twice comes back as two words in the same place; the caller merges them.

Everything that can fail here -- no PowerShell, no OCR language installed,
a script error, a timeout, output that cannot be read -- comes back as no
words at all rather than an exception. Recognition only ever *adds* boxes;
failing to add any is always a safe outcome, and one the user can see.

Nothing here imports anything Windows-only, so the parsing, scaling and
clean-up can be tested on any OS with the subprocess call faked.
"""

from __future__ import annotations

import base64
import os
import shutil
import subprocess
import sys
import tempfile

from PyQt6.QtCore import QRectF, Qt
from PyQt6.QtGui import QImage

from snipux.sensitive import RecognizedWord

# `OcrEngine.MaxImageDimension`: the engine refuses anything larger.
MAX_DIMENSION = 10000

# Images no longer than this on their longest edge get the second, doubled
# read. Capped so a large selection is never inflated into a huge temporary
# file for text that is already big enough to read.
UPSCALE_LONGEST_EDGE = 2000

TIMEOUT_SECONDS = 10.0

# `CREATE_NO_WINDOW`: without it, snipux running under `pythonw` flashes a
# console window on screen for every recognition.
_CREATE_NO_WINDOW = 0x08000000

_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.Encoding]::UTF8
try {
  Add-Type -AssemblyName System.Runtime.WindowsRuntime
  $asTask = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
    $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
  function Await($op, $type) {
    $task = $asTask.MakeGenericMethod($type).Invoke($null, @($op))
    $task.Wait(-1) | Out-Null
    $task.Result
  }
  $null = [Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime]
  $null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics.Imaging, ContentType = WindowsRuntime]
  $null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime]
  $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
  if ($null -eq $engine) { exit 0 }
  $culture = [Globalization.CultureInfo]::InvariantCulture
  $paths = @(__PATHS__)
  for ($pass = 0; $pass -lt $paths.Count; $pass++) {
    $file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($paths[$pass])) ([Windows.Storage.StorageFile])
    $stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
    try {
      $decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
      $bitmap = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
      $result = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
    } finally { $stream.Dispose() }
    $index = 0
    foreach ($line in $result.Lines) {
      foreach ($word in $line.Words) {
        $r = $word.BoundingRect
        [Console]::Out.WriteLine(
          [string]$pass + "`t" + [string]$index + "`t" +
          $r.X.ToString($culture) + "`t" + $r.Y.ToString($culture) + "`t" +
          $r.Width.ToString($culture) + "`t" + $r.Height.ToString($culture) + "`t" + $word.Text)
      }
      $index++
    }
  }
} catch { exit 1 }
"""


def available() -> bool:
    """Whether recognition can be attempted here at all.

    Deliberately cheap -- it is asked every time the overlay opens, and
    actually starting PowerShell to check would cost that open a third of a
    second. A machine with PowerShell but no OCR language simply recognises
    nothing, which `recognize` already handles.
    """
    return sys.platform == "win32" and shutil.which("powershell.exe") is not None


def recognize(image: QImage, *, runner=None, timeout: float = TIMEOUT_SECONDS) -> list[list[RecognizedWord]]:
    """Every word in `image`, grouped into the lines OCR found them in.

    Rects are in `image`'s own pixel space, whatever scaling was applied on
    the way to the engine. When the image is read at two sizes, the lines
    of both reads are returned one after the other -- lines are independent
    units to `sensitive.find_sensitive`, so nothing is joined across reads.

    `runner(command, timeout) -> (returncode, stdout_bytes)` is the process
    call, injected so tests can drive this without PowerShell.
    """
    if image.isNull() or image.width() == 0 or image.height() == 0:
        return []

    scales = _scales_for(image.width(), image.height())
    run = runner if runner is not None else _run
    paths: list[str] = []
    try:
        for scale in scales:
            sent = image if scale == 1.0 else image.scaled(
                max(1, round(image.width() * scale)),
                max(1, round(image.height() * scale)),
                Qt.AspectRatioMode.IgnoreAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            handle, path = tempfile.mkstemp(prefix="snipux-ocr-", suffix=".bmp")
            os.close(handle)
            paths.append(os.path.abspath(path))
            # BMP rather than PNG: this file lives for one call, and
            # encoding a full-desktop PNG would cost more than the
            # recognition itself.
            if not sent.save(path, "BMP"):
                return []
        code, stdout = run(_command_for(paths), timeout)
    except (OSError, subprocess.SubprocessError):
        return []
    finally:
        for path in paths:
            try:
                os.remove(path)
            except OSError:
                pass

    if code != 0:
        return []
    return _parse(stdout, scales)


def _scales_for(width: int, height: int) -> list[float]:
    """The sizes to read an image at, as multiples of its own."""
    longest = max(width, height)
    if longest > MAX_DIMENSION:
        return [MAX_DIMENSION / longest]
    if longest <= UPSCALE_LONGEST_EDGE:
        return [1.0, 2.0]
    return [1.0]


def _command_for(paths: list[str]) -> list[str]:
    # The script travels as -EncodedCommand (base64 of UTF-16LE) so no
    # quoting of the script or the paths can be got wrong by a shell; each
    # path is escaped for the single-quoted PowerShell string it lands in.
    quoted = ",".join("'" + path.replace("'", "''") + "'" for path in paths)
    script = _SCRIPT.replace("__PATHS__", quoted)
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    return [
        "powershell.exe", "-NoProfile", "-NonInteractive",
        "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded,
    ]


def _run(command: list[str], timeout: float) -> tuple[int, bytes]:
    completed = subprocess.run(
        command,
        capture_output=True,
        timeout=timeout,
        creationflags=_CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )
    return completed.returncode, completed.stdout


def _parse(stdout: bytes, scales: list[float]) -> list[list[RecognizedWord]]:
    """One output line per word: pass, line index, x, y, width, height,
    text, tab-separated. `scales[pass]` is what that read's rects are
    divided by to land back in the original image's pixels. Numbers are
    invariant-culture, so a German Windows does not write `12,5`. A line
    that does not parse is skipped rather than failing the whole result."""
    lines: dict[tuple[int, int], list[RecognizedWord]] = {}
    for raw in stdout.decode("utf-8-sig", errors="replace").splitlines():
        parts = raw.split("\t", 6)
        if len(parts) != 7 or not parts[6]:
            continue
        try:
            read, index = int(parts[0]), int(parts[1])
            scale = scales[read]
            x, y, width, height = (float(value) / scale for value in parts[2:6])
        except (ValueError, IndexError):
            continue
        lines.setdefault((read, index), []).append(
            RecognizedWord(parts[6], QRectF(x, y, width, height))
        )
    return [lines[key] for key in sorted(lines)]
